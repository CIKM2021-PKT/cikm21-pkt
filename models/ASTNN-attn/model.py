import torch
import torch.nn as nn
import torch.nn.functional as F

from utils import *

class BatchTreeEncoder(nn.Module):
    def __init__(self, vocab_size, embedding_dim, encode_dim, batch_size, use_gpu, pretrained_weight=None):
        super(BatchTreeEncoder, self).__init__()
        self.embedding = nn.Embedding(vocab_size, embedding_dim)
        self.encode_dim = encode_dim
        self.W_c = nn.Linear(embedding_dim, encode_dim)
        self.W_l = nn.Linear(encode_dim, encode_dim)
        self.W_r = nn.Linear(encode_dim, encode_dim)
        self.activation = F.relu
        self.stop = -1
        self.batch_size = batch_size
        self.use_gpu = use_gpu
        self.node_list = []
        self.batch_node = None
        # pretrained  embedding
        if pretrained_weight is not None:
            self.embedding.weight.data.copy_(torch.from_numpy(pretrained_weight))


    def traverse_mul(self, node, batch_index):
        size = len(node)
        if not size:
            return None
        batch_current = variable(torch.zeros(size, self.encode_dim), self.use_gpu)

        index, children_index = [], []
        current_node, children = [], []
        for i in range(size):
            # if node[i][0] is not -1:
            index.append(i)
            current_node.append(node[i][0])
            temp = node[i][1: 3]  #限制个数，递归部分，限制子树深度
            c_num = len(temp)
            for j in range(c_num):
                if temp[j][0] != -1:
                    if len(children_index) <= j:
                        children_index.append([i])
                        children.append([temp[j]])
                    else:
                        children_index[j].append(i)
                        children[j].append(temp[j])
            # else:
            #     batch_index[i] = -1

        batch_current = self.W_c(batch_current.index_copy(0, variable(torch.LongTensor(index), self.use_gpu),
                    self.embedding(variable(torch.LongTensor(current_node), self.use_gpu))))

        for c in range(len(children)):
            zeros = variable(torch.zeros(size, self.encode_dim), self.use_gpu)
            batch_children_index = [batch_index[i] for i in children_index[c]]
            tree = self.traverse_mul(children[c], batch_children_index)
            if tree is not None:
                batch_current += zeros.index_copy(0, variable(torch.LongTensor(children_index[c]), self.use_gpu), tree)
        # batch_current = F.tanh(batch_current)
        batch_index = [i for i in batch_index if i != -1]
        b_in = variable(torch.LongTensor(batch_index), self.use_gpu)
        self.node_list.append(self.batch_node.index_copy(0, b_in, batch_current))
        return batch_current

    def forward(self, x, bs):
        self.batch_size = bs
        self.batch_node = variable(torch.zeros(self.batch_size, self.encode_dim), self.use_gpu)
        self.node_list = []
        self.traverse_mul(x, list(range(self.batch_size)))
        self.node_list = torch.stack(self.node_list)
        return torch.max(self.node_list, 0)[0]

class MODEL(nn.Module):
    def __init__(self, num_concepts, num_problems, hidden_dim, hidden_layers,
                 concept_embed_dim, ast_embed_dim, max_tokens, ast_encode_dim,
                 ast_pretrained_weight, max_len, batch_size, gpu):
        super(MODEL, self).__init__()

        self.hidden_dim = hidden_dim
        self.hidden_layers = hidden_layers
        self.gpu = gpu
        self.num_concepts = num_concepts
        self.ast_encode_dim = ast_encode_dim
        self.batch_size = batch_size
        self.max_len = max_len
        self.concept_embed_dim = concept_embed_dim

        # DKT network
        self.p_id_embed = nn.Embedding(num_problems + 1, concept_embed_dim)

        # class "BatchTreeEncoder"
        self.encoder = BatchTreeEncoder(max_tokens, ast_embed_dim, ast_encode_dim,
                                        batch_size, gpu, ast_pretrained_weight)
        if self.gpu == 0:
            self.encoder.cuda()

        self.concept_embedding = nn.Parameter(torch.empty(num_concepts + 1, concept_embed_dim), requires_grad=True)
        self.transdim = nn.Linear(128, self.concept_embed_dim, bias=False)

        # input feature dim
        self.LSTM_feature_dim = 2 + concept_embed_dim + num_concepts + 1

        self.LSTM = nn.LSTM(input_size=self.LSTM_feature_dim, hidden_size=hidden_dim,
                            num_layers=hidden_layers, batch_first=True)
        self.predict_Linear = nn.Linear(hidden_dim, num_concepts + 1, bias=True)


    def init_embeddings(self):
        nn.init.kaiming_normal_(self.p_id_embed.weight)
        nn.init.kaiming_normal_(self.concept_embedding.data)

    def init_params(self):
        nn.init.kaiming_normal_(self.predict_Linear.weight)
        nn.init.constant_(self.predict_Linear.bias, 0)
        nn.init.kaiming_normal_(self.transdim.weight)


    def get_zeros(self, num):
        zeros = variable(torch.zeros(num, self.ast_encode_dim), self.gpu)
        return zeros

    def attention(self, c_id, c_embed, gru_input, bs, seqlen):

        gru_input = self.transdim(gru_input)
        # interactive attention
        c_embed = c_embed.unsqueeze(3).expand(bs, seqlen, self.num_concepts + 1, self.concept_embed_dim)

        # the query is concept
        # 习题知识点个数，用于c_p_attn的AvgPooling
        num = torch.sum(c_id, dim=2).unsqueeze(2)
        num = torch.masked_fill(num, num.eq(0), 1)
        concept_query = c_embed * self.concept_embedding.repeat(bs, seqlen, 1, 1)
        c_p_attn_weight = torch.bmm(concept_query.view(bs * seqlen, self.num_concepts + 1, self.concept_embed_dim),
                                    gru_input.view(bs * seqlen, self.max_len,
                                                  self.concept_embed_dim).permute(0, 2, 1))
        c_p_attn_weight = torch.softmax(c_p_attn_weight, dim=2)
        c_p_attn_out = torch.bmm(c_p_attn_weight, gru_input.view(bs * seqlen, self.max_len, self.concept_embed_dim))
        c_p_attn_out = c_p_attn_out * c_embed.view(bs * seqlen, self.num_concepts + 1, self.concept_embed_dim)
        c_p_attn_out = torch.sum(c_p_attn_out, dim=1) / num.view(-1).unsqueeze(1)
        c_p_attn_out = c_p_attn_out.view(bs, seqlen, self.concept_embed_dim)

        return c_p_attn_out


    def forward(self, p_id, c_id, ast, target_c, result, c_embed, cur_result):
        bs = p_id.shape[0]
        seqlen = p_id.shape[1]

        #####################################################################################################
        # models
        # p_id_embed = self.p_id_embed(p_id)

        batch_encodes = []
        for idx in range(bs):
            lens = [len(item) for item in ast[idx]]

            encodes = []
            for i in range(len(ast[idx])):
                for j in range(lens[i]):
                    encodes.append(ast[idx][i][j])

            encodes = self.encoder(encodes, sum(lens))
            seq, start, end = [], 0, 0
            for i in range(len(ast[idx])):
                end += lens[i]
                if self.max_len - lens[i] > 0:
                    seq.append(self.get_zeros(self.max_len - lens[i]))
                    seq.append(encodes[start: end])
                else:
                    seq.append(encodes[start: start + self.max_len])
                start = end
            encodes = torch.cat(seq)
            encodes = encodes.view(len(ast[idx]), self.max_len, -1)
            batch_encodes.append(encodes)

        gru_input = variable(torch.zeros(bs, seqlen, self.max_len, self.ast_encode_dim), self.gpu)
        for i in range(bs):
            dat = batch_encodes[i]
            gru_input[i, :dat.shape[0], :dat.shape[1], :] = dat

        attn_out = self.attention(c_id, c_embed, gru_input, bs, seqlen)

        LSTM_input = torch.cat([c_embed, attn_out, cur_result], 2)

        # LSTM
        LSTM_out, final_status = self.LSTM(LSTM_input)
        LSTM_out = LSTM_out.contiguous()

        #######################################################################################################
        # prediction

        # 多知识点，除以知识点个数
        num_concepts = torch.sum(target_c, 2).view(-1)

        prediction = self.predict_Linear(LSTM_out.view(bs * seqlen, -1))  # 去掉dropout
        prediction_1d = torch.bmm(prediction.unsqueeze(1),
                                  target_c.view(bs * seqlen, -1).unsqueeze(2)).squeeze(2)
        mask = num_concepts.gt(0)
        num_concepts = torch.masked_select(num_concepts, mask)
        filtered_pred = torch.masked_select(prediction_1d.squeeze(1), mask)
        filtered_pred = torch.div(filtered_pred, num_concepts)
        filtered_target = torch.masked_select(result.squeeze(1), mask)
        loss = F.binary_cross_entropy_with_logits(filtered_pred, filtered_target)


        return loss, torch.sigmoid(filtered_pred), filtered_target


