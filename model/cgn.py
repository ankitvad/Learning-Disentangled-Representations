import numpy as np
import torch as t
import torch.nn as nn
from torch.optim import Adam
from torch.autograd import Variable
import torch.nn.functional as F

# Our Encoder and Generator Class
from .encoder import Encoder
from .generator import Generator
from selfModules.embedding import Embedding
from utils.beam_search import Beam
# from .discriminator_sentiment import Sentiment_Discriminator

from utils.functional import kld_coef

class Controlled_Generation_Sentence(nn.Module):

    def __init__(self, config, embedding_path):

        super(Controlled_Generation_Sentence, self).__init__()

        self.config = config
        self.embedding = Embedding(self.config, embedding_path)

        self.encoder = Encoder(self.config)

        self.e2mu = nn.Linear(self.config.encoder_rnn_size*2, self.config.latent_variable_size+1)
        self.e2logvar = nn.Linear(self.config.encoder_rnn_size*2, self.config.latent_variable_size+1)

        self.generator = Generator(self.config)
        # self.sentiment_discriminator = Sentiment_Discriminator

    def train_initial_rvae(self, drop_prob, encoder_word_input=None, encoder_char_input=None, generator_word_input=None):

        use_cuda = self.embedding.word_embed.weight.is_cuda
        
        [batch_size, _] = encoder_word_input.size()
        encoder_input = self.embedding(encoder_word_input, encoder_char_input)

        context, h_0, c_0 = self.encoder(encoder_input, None)
        State = (h_0, c_0)

        mu = self.e2mu(context)
        logvar = self.e2logvar(context)

        std = t.exp(0.5*logvar)

        z = Variable(t.randn([batch_size, self.config.latent_variable_size]))

        init_prob = t.ones(batch_size, 1)*0.5
        c = Variable(t.bernoulli(init_prob), requires_grad=False)
                
        input_code = t.cat((z,c), 1)
        
        if use_cuda:
            input_code = input_code.cuda()
            
        kld = (-0.5 * t.sum(logvar - t.pow(mu,2) - t.exp(logvar) + 1, 1)).mean().squeeze()

        generator_input = self.embedding.word_embed(generator_word_input)
        out, final_state = self.generator(generator_input, input_code, drop_prob, None)

        return out, final_state, kld, mu, std

    def initial_trainer(self, data_handler):

        optimizer = Adam(self.learnable_parameters(), self.config.learning_rate)
        def train(i, batch_size, use_cuda, dropout, start_index):

            input = data_handler.gen_batch_loader.next_batch(batch_size, 'train', start_index)
            input = [Variable(t.from_numpy(var)) for var in input]
            input = [var.long() for var in input]
            input = [var.cuda() if use_cuda else var for var in input]

            [encoder_word_input, encoder_character_input, decoder_word_input, decoder_character_input, target] = input

            logits, _, kld,_ ,_ = self.train_initial_rvae(dropout,
                                  encoder_word_input, encoder_character_input,
                                  decoder_word_input)

            logits = logits.view(-1, self.config.word_vocab_size)
            target = target.view(-1)
            cross_entropy = F.cross_entropy(logits, target)

            loss = 79 * cross_entropy + kld_coef(i) * kld

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            return cross_entropy, kld, kld_coef(i)

        return train
        
    def learnable_parameters(self):
        # word_embedding is constant parameter thus it must be dropped from list of parameters for optimizer
        return [p for p in self.parameters() if p.requires_grad]

    def sample_beam_for_decoder (self, batch_loader, seq_len, seed,
                                 use_cuda, beam_size = 10, n_best = 1, samples=5):

        seed = Variable(seed)
        if use_cuda:
            seed = seed.cuda()

        # seed = seed.unsqueeze(1)
        # seed = t.cat([seed] * beam_size, 1)

        print seed
        # State see the shape
        dec_states = None
        drop_prob = 0.0
        batch_size = samples # Make this samples
        
        beam = [Beam(beam_size, batch_loader, cuda=True) for k in range(batch_size)]
        
        batch_idx = list(range(batch_size))
        remaining_sents =  batch_size
        
        for i in range(seq_len):
            
            input = t.stack(
                [b.get_current_state() for b in beam if not b.done]
            ).t().contiguous().view(1, -1)
            # input becomes (1, beam_size * batch_size)
            
            trg_emb = self.embedding.word_embed(Variable(input).transpose(1, 0))
            # trg_emb.size() => (beam_size*batch_size, 1, embedding_size)
            
            trg_h, dec_states = self.generator.only_decoder_beam(trg_emb, seed, drop_prob, dec_states)
            # trg_h.size() => (beam_size*batch_size, 1, gen_rnn_size)
            # dec_states => tuple of hidden states and cell state
            
            dec_out = trg_h.squeeze(1)
            # dec_out.size() => (beam_size*batch_size, generator_rnn_size)
            
            out = F.softmax(self.generator.fc(dec_out)).unsqueeze(0)
            # out.size() => (1, beam_size*batch_size, vocab_size)

            word_lk = out.view(
                beam_size,
                remaining_sents,
                -1
            ).transpose(0, 1).contiguous()
            # word_lk.size() => (remaining_sents, beam_size, vocab_size)
            active = []
            for b in range(batch_size):
                if beam[b].done:
                    continue

                idx = batch_idx[b]
                # beam state advance
                if not beam[b].advance(word_lk.data[idx]):
                    active += [b]
                
                for dec_state in dec_states:  # iterate over h, c
                    # layers x beam*sent x dim
                    sent_states = dec_state.view(
                        -1, beam_size, remaining_sents, dec_state.size(2)
                    )[:, :, idx] 

                    # sent_states.size() => (layers, beam_size, gen_rnn_size)
                    
                    sent_states.data.copy_(
                        sent_states.data.index_select(
                            1,
                            beam[b].get_current_origin()
                        )
                    )

            if not active:
                break

            # in this section, the sentences that are still active are
            # compacted so that the decoder is not run on completed sentences
            active_idx = t.cuda.LongTensor([batch_idx[k] for k in active])
            batch_idx = {beam: idx for idx, beam in enumerate(active)}

            def update_active(t):
                # select only the remaining active sentences
                view = t.data.view(
                    -1, remaining_sents,
                    self.config.decoder_rnn_size
                )
                new_size = list(t.size())
                new_size[-2] = new_size[-2] * len(active_idx) \
                    // remaining_sents
                return Variable(view.index_select(
                    1, active_idx
                ).view(*new_size))

            dec_states = (
                update_active(dec_states[0]),
                update_active(dec_states[1])
            )
            dec_out = update_active(dec_out)
            remaining_sents = len(active) 

         # (4) package everything up

        allHyp, allScores = [], []

        for b in range(batch_size):
            scores, ks = beam[b].sort_best()
            allScores += [scores[:n_best]]
            hyps = zip(*[beam[b].get_hyp(k) for k in ks[:n_best]])
            allHyp += [hyps]

        word_hyp = []
        result = []
                
        all_sent_codes = np.asarray(allHyp)
        print all_sent_codes.shape
        
        all_sent_codes = np.transpose(allHyp, (0,2,1))
        print all_sent_codes.shape
        print all_sent_codes
        
        all_sentences = []
        for batch in all_sent_codes:
            sentences = []
            for i_best in batch:
                sentence = ""
                for word_code in i_best:
                    word = batch_loader.decode_word(word_code)
                    if word == batch_loader.end_token:
                        break
                    sentence += ' ' + word
                sentences.append(sentence)

            all_sentences.extend(sentences)
        

        """
        for hyp in allHyp:

            sentence = []
            
            for i_step in range(seq_len):

                for idx in range(n_best):

                    if sentence[i_step][idx] == batch_loader.end_token:
                        continue;

                    
                for word_idx in hyp[i_step]:

                    if sentence[i_step][]
                    word = batch_loader.decode_word(word_idx)
                    # temp = map(batch_loader.decode_word, hyp[i_step])

                    if word == batch_loader.end_token:
                        break

                result += ' ' + word

                
                print temp
                word_hyp += temp
                
        """
            
        return all_sentences, allScores 

    def sample(self, data_handler, config, use_cuda=True):

        samp = 2
        seed = t.randn([samp, config.latent_variable_size+1])
        sentences, result_score = self.sample_beam_for_decoder(data_handler.gen_batch_loader, config.max_seq_len, seed, use_cuda, samples=samp)

        print len(sentences)
        for s in sentences:
            print s
