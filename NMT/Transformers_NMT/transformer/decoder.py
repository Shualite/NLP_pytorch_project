"""

@file  : decoder.py

@author: xiaolu

@time  : 2019-12-25

"""
import torch
import torch.nn as nn
import torch.nn.functional as F

from config import Config
from .attention import MultiHeadAttention
from .module import PositionalEncoding, PositionwiseFeedForward
from .utils import get_attn_key_pad_mask, get_attn_pad_mask, get_non_pad_mask, get_subsequent_mask, pad_list


class Decoder(nn.Module):
    '''
    A decoder model with self attention mechanism.
    '''
    def __init__(self, sos_id=Config.sos_id, eos_id=Config.eos_id, n_tgt_vocab=Config.n_tgt_vocab, d_word_vec=512,
                 n_layers=6, n_head=8, d_k=64, d_v=64, d_model=512, d_inner=2048, dropout=0.1,
                 tgt_emb_prj_weight_sharing=True, pe_maxlen=5000):
        '''
        :param sos_id: 起始标志
        :param eos_id: 结束标志
        :param n_tgt_vocab: 目标词表的大小
        :param d_word_vec: 词嵌入的大小
        :param n_layers: 几个解码块
        :param n_head: 注意力中用几个头
        :param d_k: q, k 向量的维度
        :param d_v: v向量的维度
        :param d_model: 词嵌入的维度
        :param d_inner: 前馈网络中间那一步的维度
        :param dropout: dropout率
        :param tgt_emb_prj_weight_sharing:
        :param pe_maxlen:
        '''
        super(Decoder, self).__init__()
        # parameters
        self.sos_id = sos_id  # Start of Sentence
        self.eos_id = eos_id  # End of Sentence
        self.n_tgt_vocab = n_tgt_vocab
        self.d_word_vec = d_word_vec
        self.n_layers = n_layers  # 几层解码
        self.n_head = n_head
        self.d_k = d_k
        self.d_v = d_v
        self.d_model = d_model
        self.d_inner = d_inner
        self.dropout = dropout
        self.tgt_emb_prj_weight_sharing = tgt_emb_prj_weight_sharing
        self.pe_maxlen = pe_maxlen

        self.tgt_word_emb = nn.Embedding(n_tgt_vocab, d_word_vec)
        self.pos_emb = PositionalEncoding(d_model, max_len=pe_maxlen)
        self.dropout = nn.Dropout(dropout)

        self.layer_stack = nn.ModuleList([
            DecoderLayer(d_model, d_inner, n_head, d_k, d_v, dropout=dropout)
            for _ in range(n_layers)])

        self.tgt_word_prj = nn.Linear(d_model, n_tgt_vocab, bias=False)  # 解码的输出
        nn.init.xavier_normal_(self.tgt_word_prj.weight)

        # 解码的输出全连接和词嵌入的全连接是否搞成权重共享 也就是用词嵌入的权重来初始化最后输出的全连接网络
        if tgt_emb_prj_weight_sharing:
            # Share the weight matrix between target word embedding & the final logit dense layer
            self.tgt_word_prj.weight = self.tgt_word_emb.weight
            self.x_logit_scale = (d_model ** -0.5)
        else:
            self.x_logit_scale = 1.

    def preprocess(self, padded_input):
        '''
        Generate decoder input and output label from padded_input
        Add <sos> to decoder input, and add <eos> to decoder output label
        生成解码的输入和输出
        :param padded_input:
        :return:
        '''
        ys = [y[y != Config.IGNORE_ID] for y in padded_input]

        eos = ys[0].new([self.eos_id])
        sos = ys[0].new([self.sos_id])

        ys_in = [torch.cat([sos, y], dim=0) for y in ys]
        ys_out = [torch.cat([y, eos], dim=0) for y in ys]

        ys_in_pad = pad_list(ys_in, self.eos_id)
        ys_out_pad = pad_list(ys_out, Config.IGNORE_ID)
        assert ys_in_pad.size() == ys_out_pad.size()

        return ys_in_pad, ys_out_pad

    def forward(self, padded_input, encoder_padded_outputs, encoder_input_lengths, return_attns=False):
        # self.decoder(padded_target, encoder_padded_outputs, input_lengths)
        """
        Args:
            padded_input: batch_size x max_len
            encoder_padded_outputs: batch_size x max_len x hidden_size
        Returns:
        """
        dec_slf_attn_list, dec_enc_attn_list = [], []
        
        '''
            1. token转embdding，添加PE
        '''
        ys_in_pad, ys_out_pad = self.preprocess(padded_input)   # ys_in_pad 加了[S]符号，torch.Size([B, T])；ys_out_pad 加了[END]符号，torch.Size([B, T])
        ys_in_fuse_embed = self.tgt_word_emb(ys_in_pad) * self.x_logit_scale 
        ys_in_fuse_embed += self.pos_emb(ys_in_pad)
        dec_output = self.dropout(ys_in_fuse_embed)     # dec_output，torch.Size([128, 24, 512])
        
        '''
            2. 为了得到pad 位置
        '''
        non_pad_mask = get_non_pad_mask(ys_in_pad, pad_idx=self.eos_id)                 # pad 部分，置零，torch.Size([B, T, 1]) 
        dec_enc_attn_mask = get_attn_pad_mask(padded_input=encoder_padded_outputs,      # dec_enc_attn_mask，torch.Size([B, 1, encoder_max_len]).expand(-1, ys_in_pad.size(1), -1)
                                              input_lengths=encoder_input_lengths,
                                              expand_length=ys_in_pad.size(1))    

        '''
            3. slf_attn_mask
                encoder: 仅需要考虑pad位置 (不需要做上三角矩阵)
                decoder: pad mask or 上三角矩阵
        '''
        slf_attn_mask_subseq = get_subsequent_mask(ys_in_pad)                           # 上三角矩阵，对角线为0，torch.Size([B, T, T])
        slf_attn_mask_keypad = get_attn_key_pad_mask(ys_in_pad, ys_in_pad, self.eos_id) # pad mask，设置True，torch.Size([B, 1, T]).expand(-1, T, -1)
        slf_attn_mask = (slf_attn_mask_keypad + slf_attn_mask_subseq).gt(0)             # 上三角为True 且 mask pad 部分也为True
        
        for dec_layer in self.layer_stack:
            dec_output, dec_slf_attn, dec_enc_attn = dec_layer(
                dec_output, encoder_padded_outputs,
                non_pad_mask=non_pad_mask,
                slf_attn_mask=slf_attn_mask,
                dec_enc_attn_mask=dec_enc_attn_mask)

            if return_attns:
                dec_slf_attn_list += [dec_slf_attn]
                dec_enc_attn_list += [dec_enc_attn]

        # before softmax
        seq_logit = self.tgt_word_prj(dec_output)

        # Return
        pred, gold = seq_logit, ys_out_pad

        if return_attns:
            return pred, gold, dec_slf_attn_list, dec_enc_attn_list
        return pred, gold

    def recognize_beam(self, encoder_outputs, char_list):
        """Beam search, decode one utterence now.
        Args:
            encoder_outputs: T x H
            char_list: list of character
            args: args.beam
        Returns:
            nbest_hyps:
        """
        # search params
        beam = 5
        nbest = 1
        maxlen = 100

        encoder_outputs = encoder_outputs.unsqueeze(0)

        # prepare sos
        ys = torch.ones(1, 1).fill_(self.sos_id).type_as(encoder_outputs).long()

        # yseq: 1xT
        hyp = {'score': 0.0, 'yseq': ys}
        hyps = [hyp]
        ended_hyps = []

        for i in range(maxlen):
            hyps_best_kept = []
            for hyp in hyps:
                ys = hyp['yseq']  # 1 x i

                # -- Prepare masks
                non_pad_mask = torch.ones_like(ys).float().unsqueeze(-1)  # 1xix1
                slf_attn_mask = get_subsequent_mask(ys)

                # -- Forward
                dec_output = self.dropout(
                    self.tgt_word_emb(ys) * self.x_logit_scale +
                    self.positional_encoding(ys))

                for dec_layer in self.layer_stack:
                    dec_output, _, _ = dec_layer(
                        dec_output, encoder_outputs,
                        non_pad_mask=non_pad_mask,
                        slf_attn_mask=slf_attn_mask,
                        dec_enc_attn_mask=None)

                seq_logit = self.tgt_word_prj(dec_output[:, -1])

                local_scores = F.log_softmax(seq_logit, dim=1)
                # topk scores
                local_best_scores, local_best_ids = torch.topk(
                    local_scores, beam, dim=1)

                for j in range(beam):
                    new_hyp = {}
                    new_hyp['score'] = hyp['score'] + local_best_scores[0, j]
                    new_hyp['yseq'] = torch.ones(1, (1 + ys.size(1))).type_as(encoder_outputs).long()
                    new_hyp['yseq'][:, :ys.size(1)] = hyp['yseq']
                    new_hyp['yseq'][:, ys.size(1)] = int(local_best_ids[0, j])
                    # will be (2 x beam) hyps at most
                    hyps_best_kept.append(new_hyp)

                hyps_best_kept = sorted(hyps_best_kept,
                                        key=lambda x: x['score'],
                                        reverse=True)[:beam]
            # end for hyp in hyps
            hyps = hyps_best_kept

            # add eos in the final loop to avoid that there are no ended hyps
            if i == maxlen - 1:
                for hyp in hyps:
                    hyp['yseq'] = torch.cat([hyp['yseq'],
                                             torch.ones(1, 1).fill_(self.eos_id).type_as(encoder_outputs).long()],
                                            dim=1)

            # add ended hypothes to a final list, and removed them from current hypothes
            # (this will be a probmlem, number of hyps < beam)
            remained_hyps = []
            for hyp in hyps:
                if hyp['yseq'][0, -1] == self.eos_id:
                    ended_hyps.append(hyp)
                else:
                    remained_hyps.append(hyp)

            hyps = remained_hyps

        # end for i in range(maxlen)
        nbest_hyps = sorted(ended_hyps, key=lambda x: x['score'], reverse=True)[
                     :min(len(ended_hyps), nbest)]
        # compitable with LAS implementation
        for hyp in nbest_hyps:
            hyp['yseq'] = hyp['yseq'][0].cpu().numpy().tolist()
        return nbest_hyps


class DecoderLayer(nn.Module):
    '''
    Compose with three layers
    '''
    def __init__(self, d_model, d_inner, n_head, d_k, d_v, dropout=0.1):
        super(DecoderLayer, self).__init__()
        self.slf_attn = MultiHeadAttention(n_head, d_model, d_k, d_v, dropout=dropout)
        self.enc_attn = MultiHeadAttention(n_head, d_model, d_k, d_v, dropout=dropout)
        self.pos_ffn = PositionwiseFeedForward(d_model, d_inner, dropout=dropout)

    def forward(self, dec_input, enc_output, non_pad_mask=None, slf_attn_mask=None, dec_enc_attn_mask=None):
        import ipdb;ipdb.set_trace()
        
        # 解码输入之间的attention
        dec_output, dec_slf_attn = self.slf_attn(
            dec_input, dec_input, dec_input, mask=slf_attn_mask
        )   # 解码输入之间的attention  所以带进去的mask矩阵是个上三角阵
        dec_output *= non_pad_mask  # 把padding那块算的attention裁剪掉

        # 解码与编码输出的attention
        dec_output, dec_enc_attn = self.enc_attn(
            dec_output, enc_output, enc_output, mask=dec_enc_attn_mask
        )
        dec_output *= non_pad_mask

        dec_output = self.pos_ffn(dec_output)
        dec_output *= non_pad_mask

        return dec_output, dec_slf_attn, dec_enc_attn
