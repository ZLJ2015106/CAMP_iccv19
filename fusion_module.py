import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist
import torch.nn.init
import torchvision.models as models
from torch.autograd import Variable
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
from torch.nn.utils.clip_grad import clip_grad_norm
import numpy as np
from collections import OrderedDict
import torch.nn.functional as F


from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
import logging
import torch.backends.cudnn as cudnn
import pickle
from math import sqrt

def l2norm(X, dim=-1, eps=1e-8):
    """L2-normalize columns of X
    """
    norm = torch.pow(X, 2).sum(dim=dim, keepdim=True).sqrt() + eps
    X = torch.div(X, norm)  
    return X


def sum_attention(nnet, query, value, mask=None, dropout=None):
    scores = nnet(query).transpose(-2, -1)   ##对区域-文字亲和矩阵进行转置
    if mask is not None:
        scores.data.masked_fill_(mask.data.eq(0), -1e9) ## mask==0的地方，对scores中相应位置用-1e9替代，scores中其他值保持不变
    p_attn = F.softmax(scores, dim=-1)  ## dim = 1与dim = -1都是按行计算softmax，dim = 0是按列计算
    if dropout is not None:
        p_attn = dropout(p_attn)
    return torch.matmul(p_attn, value), p_attn  


def qkv_attention(query, key, value, mask=None, dropout=None):
    d_k = query.size(-1)  ## size用来统计矩阵元素个数，或矩阵某一维上的元素个数的函数
    scores = torch.matmul(query, key.transpose(-2, -1)) / sqrt(d_k)  ## 计算公式（3）
    if mask is not None:
        scores.data.masked_fill_(mask.data.eq(0), -1e9)   ##在mask值为0的位置处用-1e9填充。mask的元素个数需和本tensor相同，但尺寸可以不同
    p_attn = F.softmax(scores, dim=-1)
    if dropout is not None:
        p_attn = dropout(p_attn)

    return torch.matmul(p_attn, value), p_attn  ##这个函数没对value转置，可能传进来已经转置了

class SummaryAttn(nn.Module):

	def __init__(self, dim, num_attn, dropout, is_cat=False):
		super(SummaryAttn, self).__init__()
		self.linear = nn.Sequential(             ## 将按照在构造函数中传递的顺序添加到模块中
				nn.Linear(dim, dim),
				nn.ReLU(inplace=True),
				nn.Linear(dim, num_attn),
			)
		self.h = num_attn
		self.is_cat = is_cat
		self.attn = None
		self.dropout = nn.Dropout(p=dropout) if dropout > 0 else None

	def forward(self, query, value, mask=None):
		if mask is not None:
			mask = mask.unsqueeze(-2)
		batch = query.size(0)  ##取出query的第一维度

		weighted, self.attn = sum_attention(self.linear, query, value, mask=mask, dropout=self.dropout)
		weighted = weighted if self.is_cat else weighted.mean(dim=-2)

		return weighted  ##返回 公式（3）输出

class CrossAttention(nn.Module):
    """ TBD...
    """
    def __init__(self, dim, num_attn, dropout, reduce_func="self_attn"):
        super(CrossAttention, self).__init__()
        self.dim = dim
        self.h = num_attn
        self.dropout = nn.Dropout(p=dropout) if dropout > 0 else None

        self.reduce_func = reduce_func

        self.img_key_fc = nn.Linear(dim, dim, bias=False)
        self.txt_key_fc = nn.Linear(dim, dim, bias=False)

        if reduce_func == "mean":
            self.reduce_layer = torch.mean
        elif reduce_func == "self_attn":
            self.reduce_layer_1 = SummaryAttn(dim, num_attn, dropout)
            self.reduce_layer_2 = SummaryAttn(dim, num_attn, dropout)
        
        self.init_weights()
        print("CrossAttention module init success!")

    def init_weights(self):
        """Xavier initialization for the fully connected layer
        """
        r = np.sqrt(6.) / np.sqrt(self.dim +
                                  self.dim)
        self.img_key_fc.weight.data.uniform_(-r, r)
        self.txt_key_fc.weight.data.uniform_(-r, r)

    def forward(self, v1, v2, get_score=True, keep=None, mask=None):
        if keep == "words":
            v2 = v2.squeeze(0)## 只能压缩维度为1的维
            mask = mask.squeeze(0)
        elif keep == "regions":
            v1 = v1.squeeze(0) 


        k1 = self.img_key_fc(v1) ##对图像区域做线性变换公式（1）
        k2 = self.txt_key_fc(v2)  ##对words做线性变换公式（1）
        batch_size_v1 = v1.size(0)  ##取出第一维，区域数量
        batch_size_v2 = v2.size(0)   ##取出第一维，word数量

        v1 = v1.unsqueeze(1).expand(-1, batch_size_v2, -1, -1)  ##先在区域级图像v1的第1维增加一个维度，比如原来是（2,3,1），现在变为（2,1,3,1） 通道数目原来是1，expand将通道数目扩展为batch_size_v2个
        k1 = k1.unsqueeze(1).expand(-1, batch_size_v2, -1, -1)  
        v2 = v2.unsqueeze(0).expand(batch_size_v1, -1, -1, -1)   ##word的batch设置与imag的batch相同
        k2 = k2.unsqueeze(0).expand(batch_size_v1, -1, -1, -1)

        weighted_v1, attn_1 = qkv_attention(k2, k1, v1)  ##求出公式（3）视觉在文本维度下的表示 da×1
        if mask is not None:
            weighted_v2, attn_2 = qkv_attention(k1, k2, v2, mask.unsqueeze(-2))
        else:
            weighted_v2, attn_2 = qkv_attention(k1, k2, v2)  ##求出公式（4）文本在视觉维度下的表示 dv×1

        fused_v1 = weighted_v2
        fused_v2 = weighted_v1

        

        if self.reduce_func == "self_attn":
            co_v1 = self.reduce_layer_1(fused_v1, fused_v1)
            co_v2 = self.reduce_layer_2(fused_v2, fused_v2, mask)
            co_v1 = l2norm(co_v1)
            co_v2 = l2norm(co_v2)
        else:
            co_v1 = self.reduce_func(co_v1, dim=-2)
            co_v2 = self.reduce_func(co_v2, dim=-2)
            co_v1 = l2norm(co_v1)
            co_v2 = l2norm(co_v2)

        if get_score:
            score = (co_v1 * co_v2).sum(dim=-1)
            if keep == "regions":
                score = score.transpose(0, 1)
            return score
        else:
            return torch.cat((co_v1, co_v2), dim=-1)


class GatedFusion(nn.Module):  ## 又把CrossAttention网络中的内容计算了一遍，实现了所有功能，所以只要有这个网络可以直接计算最终的匹配得分
    def __init__(self, dim, num_attn, dropout=0.01, reduce_func="self_attn", fusion_func="concat"):
        super(GatedFusion, self).__init__()
        self.dim = dim
        self.h = num_attn
        
        self.dropout = nn.Dropout(p=dropout) if dropout > 0 else None

        self.reduce_func = reduce_func
        self.fusion_func = fusion_func

        self.img_key_fc = nn.Linear(dim, dim, bias=False)
        self.txt_key_fc = nn.Linear(dim, dim, bias=False)
      

        in_dim = dim
        if fusion_func == "sum":
           in_dim = dim 
        elif fusion_func == "concat":
           in_dim = 2 * dim
        else:
           raise NotImplementedError('Only support sum or concat fusion') 

        self.fc_1 = nn.Sequential(
                        nn.Linear(in_dim, dim),
                        nn.ReLU(inplace=True),
                        nn.Dropout(p=dropout),)   ##公式（6）中的Fv，由线性层和非线性激活函数组成的可学习变换
        self.fc_2 = nn.Sequential(
                        nn.Linear(in_dim, dim),
                        nn.ReLU(inplace=True),
                        nn.Dropout(p=dropout),)   ##公式（6）中的Fv，由线性层和非线性激活函数组成的可学习变换
        self.fc_out = nn.Sequential(
                        nn.Linear(in_dim, dim),
                        nn.ReLU(inplace=True),
                        nn.Dropout(p=dropout),
                        nn.Linear(dim, 1),
                        nn.Sigmoid(),    ## 最后融合，预测之前对融合结果做一次MLP
                        )

        if reduce_func == "mean":
            self.reduce_layer = torch.mean
        elif reduce_func == "self_attn":
            self.reduce_layer_1 = SummaryAttn(dim, num_attn, dropout)
            self.reduce_layer_2 = SummaryAttn(dim, num_attn, dropout)
        
        self.init_weights()
        print("GatedFusion module init success!")

    def init_weights(self):
        """Xavier initialization for the fully connected layer
        """
        r = np.sqrt(6.) / np.sqrt(self.dim +
                                  self.dim)
        self.img_key_fc.weight.data.uniform_(-r, r)
        self.txt_key_fc.weight.data.uniform_(-r, r)
        self.fc_1[0].weight.data.uniform_(-r, r)
        self.fc_1[0].bias.data.fill_(0)
        self.fc_2[0].weight.data.uniform_(-r, r)
        self.fc_2[0].bias.data.fill_(0)
        self.fc_out[0].weight.data.uniform_(-r, r)
        self.fc_out[0].bias.data.fill_(0)
        self.fc_out[3].weight.data.uniform_(-r, r)
        self.fc_out[3].bias.data.fill_(0)

    def forward(self, v1, v2, get_score=True, keep=None, mask=None):
        if keep == "words":
            v2 = v2.squeeze(0) 
            mask = mask.squeeze(0)
        elif keep == "regions":
            v1 = v1.squeeze(0)


        k1 = self.img_key_fc(v1)
        k2 = self.txt_key_fc(v2)
        batch_size_v1 = v1.size(0)
        batch_size_v2 = v2.size(0)

        v1 = v1.unsqueeze(1).expand(-1, batch_size_v2, -1, -1)
        k1 = k1.unsqueeze(1).expand(-1, batch_size_v2, -1, -1)
        v2 = v2.unsqueeze(0).expand(batch_size_v1, -1, -1, -1)
        k2 = k2.unsqueeze(0).expand(batch_size_v1, -1, -1, -1)

        weighted_v1, attn_1 = qkv_attention(k2, k1, v1)
        if mask is not None:
            weighted_v2, attn_2 = qkv_attention(k1, k2, v2, mask.unsqueeze(-2))
        else:
            weighted_v2, attn_2 = qkv_attention(k1, k2, v2)
  
        gate_v1 = F.sigmoid((v1 * weighted_v2).sum(dim=-1)).unsqueeze(-1)  ##生成门值G,并按行求和维数减1，最后再增加最后一维，变成和原来维数一样，因为后面要做对应元素相乘，248行
        gate_v2 = F.sigmoid((v2 * weighted_v1).sum(dim=-1)).unsqueeze(-1)  ##增加维数，只是改变形状，tensor数据量不变，数据值不变
        #gate_v1 = F.sigmoid((v1 * weighted_v2))
        #gate_v2 = F.sigmoid((v2 * weighted_v1))
        if self.fusion_func == "sum":   ##fusion_func 表示基本的融合形式，就是怎么把学习到的两种模态的关系放在一起，这里一种是原始信息和获取信息求和，一种是直接串联
            fused_v1 = (v1 + weighted_v2)* gate_v1
            fused_v2 = (v2 + weighted_v1)* gate_v2
        elif self.fusion_func == "concat":
            fused_v1 = torch.cat((v1, weighted_v2), dim=-1)* gate_v1  
            fused_v2 = torch.cat((v2, weighted_v1), dim=-1)* gate_v2

        co_v1 = self.fc_1(fused_v1) + v1  ##公式6
        co_v2 = self.fc_2(fused_v2) + v2  ##公式6

        if self.reduce_func == "self_attn":
            co_v1 = self.reduce_layer_1(co_v1, co_v1)
            co_v2 = self.reduce_layer_2(co_v2, co_v2, mask)
            #co_v1 = l2norm(co_v1)
            #co_v2 = l2norm(co_v2)
        else:
            co_v1 = self.reduce_func(co_v1, dim=-2)  ##reduce_func这个函数的作用是？
            co_v2 = self.reduce_func(co_v2, dim=-2)   
            co_v1 = l2norm(co_v1)
            co_v2 = l2norm(co_v2)

        if get_score:
            if self.fusion_func == "sum":
                 score = self.fc_out(co_v1 + co_v2).squeeze(dim=-1)
            elif self.fusion_func == "concat":
                 score = self.fc_out(torch.cat((co_v1, co_v2), dim=-1)).squeeze(dim=-1)  ##得到图像文本的matching——score
            if keep == "regions":
                score = score.transpose(0, 1)
            #mean_gate = gate_v1.mean(dim=-1).mean(dim=-1) + gate_v2.mean(dim=-1).mean(dim=-1)
            return score
        else:
            return torch.cat((co_v1, co_v2), dim=-1)



class CrossAttentionNew(nn.Module):
    """ TBD...
    """
    def __init__(self, dim, num_attn, dropout, reduce_func="mean"):
        super(CrossAttentionNew, self).__init__()
        self.dim = dim
        self.h = num_attn
        self.dropout = nn.Dropout(p=dropout) if dropout > 0 else None

        self.reduce_func = reduce_func

        self.img_key_fc = nn.Linear(dim, dim, bias=False)
        self.txt_key_fc = nn.Linear(dim, dim, bias=False)

        self.img_query_fc = nn.Linear(dim, dim, bias=False)
        self.txt_query_fc = nn.Linear(dim, dim, bias=False)

        self.weighted_img_key_fc = nn.Linear(dim, dim, bias=False)
        self.weighted_txt_key_fc = nn.Linear(dim, dim, bias=False)

        self.weighted_img_query_fc = nn.Linear(dim, dim, bias=False)
        self.weighted_txt_query_fc = nn.Linear(dim, dim, bias=False)

        if reduce_func == "mean":
            self.reduce_layer = torch.mean
        elif reduce_func == "self_attn":
            self.reduce_layer_1 = SummaryAttn(dim, num_attn, dropout)
            self.reduce_layer_2 = SummaryAttn(dim, num_attn, dropout)
        
        self.init_weights()
        print("CrossAttention module init success!")

    def init_weights(self):
        """Xavier initialization for the fully connected layer
        """
        r = np.sqrt(6.) / np.sqrt(self.dim +
                                  self.dim)
        self.img_key_fc.weight.data.uniform_(-r, r)
        self.txt_key_fc.weight.data.uniform_(-r, r)

    def forward(self, v1, v2, get_score=True, keep=None, mask=None):
        if keep == "words":
            v2 = v2.squeeze(0)
            mask = mask.squeeze(0)
        elif keep == "regions":
            v1 = v1.squeeze(0)


        k1 = self.img_key_fc(v1)
        k2 = self.txt_key_fc(v2)
        q1 = self.img_query_fc(v1) ##q1=k1
        q2 = self.txt_query_fc(v2)  ##q2=k2
        batch_size_v1 = v1.size(0)
        batch_size_v2 = v2.size(0)

        v1 = v1.unsqueeze(1).expand(-1, batch_size_v2, -1, -1)  
        k1 = k1.unsqueeze(1).expand(-1, batch_size_v2, -1, -1)
        q1 = q1.unsqueeze(1).expand(-1, batch_size_v2, -1, -1)
        v2 = v2.unsqueeze(0).expand(batch_size_v1, -1, -1, -1)
        k2 = k2.unsqueeze(0).expand(batch_size_v1, -1, -1, -1)
        q2 = q2.unsqueeze(0).expand(batch_size_v1, -1, -1, -1)

        weighted_v1, attn_1 = qkv_attention(q2, k1, v1)
        if mask is not None:
            weighted_v2, attn_2 = qkv_attention(q1, k2, v2, mask.unsqueeze(-2))
        else:
            weighted_v2, attn_2 = qkv_attention(q1, k2, v2)

        weighted_v2_q = self.weighted_txt_query_fc(weighted_v2)
        weighted_v2_k = self.weighted_txt_key_fc(weighted_v2)

        weighted_v1_q = self.weighted_img_query_fc(weighted_v1)
        weighted_v1_k = self.weighted_img_key_fc(weighted_v1)

        fused_v1, _ = qkv_attention(weighted_v2_q, weighted_v2_k, weighted_v2)
        if mask is not None:
            fused_v2, _ = qkv_attention(weighted_v1_q, weighted_v1_k, weighted_v1, mask.unsqueeze(-2))
        else:
            fused_v2, _ = qkv_attention(weighted_v1_q, weighted_v1_k, weighted_v1)

        #fused_v1 = l2norm(fused_v1)
        #fused_v2 = l2norm(fused_v2)

        if self.reduce_func == "self_attn":
            co_v1 = self.reduce_layer_1(fused_v1, fused_v1)
            co_v2 = self.reduce_layer_2(fused_v2, fused_v2, mask)
            co_v1 = l2norm(co_v1)
            co_v2 = l2norm(co_v2)
        else:
            co_v1 = self.reduce_layer(fused_v1, dim=-2)
            co_v2 = self.reduce_layer(fused_v2, dim=-2)
            co_v1 = l2norm(co_v1)
            co_v2 = l2norm(co_v2)

        if get_score:
            score = (co_v1 * co_v2).sum(dim=-1)  ##分数是对应相乘 然后按行求和
            if keep == "regions":
                score = score.transpose(0, 1)
            return score
        else:
            return torch.cat((co_v1, co_v2), dim=-1)




class GatedFusionNew(nn.Module):
    def __init__(self, dim, num_attn, dropout=0.01, reduce_func="self_attn", fusion_func="concat"):
        super(GatedFusionNew, self).__init__()
        self.dim = dim
        self.h = num_attn
        
        self.dropout = nn.Dropout(p=dropout) if dropout > 0 else None

        self.reduce_func = reduce_func
        self.fusion_func = fusion_func


        self.img_key_fc = nn.Linear(dim, dim, bias=False)
        self.txt_key_fc = nn.Linear(dim, dim, bias=False)

        self.img_query_fc = nn.Linear(dim, dim, bias=False)
        self.txt_query_fc = nn.Linear(dim, dim, bias=False)

        self.weighted_img_key_fc = nn.Linear(dim, dim, bias=False)
        self.weighted_txt_key_fc = nn.Linear(dim, dim, bias=False)

        self.weighted_img_query_fc = nn.Linear(dim, dim, bias=False)
        self.weighted_txt_query_fc = nn.Linear(dim, dim, bias=False)
      

        in_dim = dim
        if fusion_func == "sum":
           in_dim = dim 
        elif fusion_func == "concat":
           in_dim = 2 * dim
        else:
           raise NotImplementedError('Only support sum or concat fusion') 

        """self.fc_gate_1 = nn.Sequential(
                            nn.Linear(in_dim, in_dim, bias=False),
                            #nn.ReLU(inplace=True),
                            #nn.Dropout(p=dropout),
                            #nn.Linear(dim, 1),
                            nn.Sigmoid(),
                            )
        self.fc_gate_2 = nn.Sequential(
                            nn.Linear(in_dim, in_dim, bias=False),
                            #nn.ReLU(inplace=True),
                            #nn.Dropout(p=dropout),
                            #nn.Linear(dim, 1),
                            nn.Sigmoid(),
                            )"""

        self.fc_1 = nn.Sequential(
                        nn.Linear(in_dim, dim, bias=False),
                        nn.ReLU(inplace=True),
                        nn.Dropout(p=dropout),)
        self.fc_2 = nn.Sequential(
                        nn.Linear(in_dim, dim, bias=False),
                        nn.ReLU(inplace=True),
                        nn.Dropout(p=dropout),)
        self.fc_out = nn.Sequential(
                        nn.Linear(in_dim, dim),
                        nn.ReLU(inplace=True),
                        nn.Dropout(p=dropout),
                        nn.Linear(dim, 1),
                        nn.Sigmoid(),
                        )

        if reduce_func == "mean":
            self.reduce_layer = torch.mean
        elif reduce_func == "self_attn":
            #self.reduce_layer_1 = SummaryAttn(dim, num_attn, dropout, is_cat=True)
            #self.reduce_layer_2 = SummaryAttn(dim, num_attn, dropout, is_cat=True)
            self.final_reduce_1 = SummaryAttn(dim, num_attn, dropout)
            self.final_reduce_2 = SummaryAttn(dim, num_attn, dropout)
        
        self.init_weights()
        print("GatedFusion module init success!")

    def init_weights(self):
        """Xavier initialization for the fully connected layer
        """
        r = np.sqrt(6.) / np.sqrt(self.dim +
                                  self.dim)
        self.img_key_fc.weight.data.uniform_(-r, r)
        self.txt_key_fc.weight.data.uniform_(-r, r)
        self.fc_1[0].weight.data.uniform_(-r, r)
        #self.fc_1[0].bias.data.fill_(0)
        self.fc_2[0].weight.data.uniform_(-r, r)
        #self.fc_2[0].bias.data.fill_(0)
        self.fc_out[0].weight.data.uniform_(-r, r)
        self.fc_out[0].bias.data.fill_(0)
        self.fc_out[3].weight.data.uniform_(-r, r)
        self.fc_out[3].bias.data.fill_(0)

    def forward(self, v1, v2, get_score=True, keep=None, mask=None):
        if keep == "words":
            v2 = v2.squeeze(0)
            mask = mask.squeeze(0)
        elif keep == "regions":
            v1 = v1.squeeze(0)


        k1 = self.img_key_fc(v1)
        k2 = self.txt_key_fc(v2)
        q1 = self.img_query_fc(v1)  ## q1 = k1
        q2 = self.txt_query_fc(v2)  ## q2 = k2
        batch_size_v1 = v1.size(0)
        batch_size_v2 = v2.size(0)

        v1 = v1.unsqueeze(1).expand(-1, batch_size_v2, -1, -1)
        k1 = k1.unsqueeze(1).expand(-1, batch_size_v2, -1, -1)
        q1 = q1.unsqueeze(1).expand(-1, batch_size_v2, -1, -1)
        v2 = v2.unsqueeze(0).expand(batch_size_v1, -1, -1, -1)
        k2 = k2.unsqueeze(0).expand(batch_size_v1, -1, -1, -1)
        q2 = q2.unsqueeze(0).expand(batch_size_v1, -1, -1, -1)

        weighted_v1, attn_1 = qkv_attention(q2, k1, v1)  ## 又把cross关系重新计算，所以只要有这个网络就能实现全部框架
        if mask is not None:
            weighted_v2, attn_2 = qkv_attention(q1, k2, v2, mask.unsqueeze(-2))
        else:
            weighted_v2, attn_2 = qkv_attention(q1, k2, v2)

        weighted_v2_q = self.weighted_txt_query_fc(weighted_v2)
        weighted_v2_k = self.weighted_txt_key_fc(weighted_v2)

        weighted_v1_q = self.weighted_img_query_fc(weighted_v1)  ##把qkv得到的分数做了一次线性映射
        weighted_v1_k = self.weighted_img_key_fc(weighted_v1)


        fused_v1, _ = qkv_attention(weighted_v2_q, weighted_v2_k, weighted_v2)
        if mask is not None:
            fused_v2, _ = qkv_attention(weighted_v1_q, weighted_v1_k, weighted_v1, mask.unsqueeze(-2))
        else:
            fused_v2, _ = qkv_attention(weighted_v1_q, weighted_v1_k, weighted_v1)

        fused_v1 = l2norm(fused_v1)
        fused_v2 = l2norm(fused_v2)

        gate_v1 = F.sigmoid((v1 * fused_v1).sum(dim=-1)).unsqueeze(-1)
        gate_v2 = F.sigmoid((v2 * fused_v2).sum(dim=-1)).unsqueeze(-1)

        if self.fusion_func == "sum": 
            #gate_v1 = self.fc_gate_1(v1 + fused_v1)
            #gate_v2 = self.fc_gate_2(v2 + fused_v2)
            co_v1 = (v1 + fused_v1) * gate_v1
            co_v2 = (v2 + fused_v2) * gate_v2
        elif self.fusion_func == "concat":
            #gate_v1 = self.fc_gate_1(torch.cat((v1, fused_v1), dim=-1))
            #gate_v2 = self.fc_gate_2(torch.cat((v2, fused_v2), dim=-1))
            co_v1 = torch.cat((v1, fused_v1), dim=-1) * gate_v1
            co_v2 = torch.cat((v2, fused_v2), dim=-1) * gate_v2

        co_v1 = self.fc_1(co_v1) + v1
        co_v2 = self.fc_2(co_v2) + v2

        if self.reduce_func == "self_attn":
            co_v1 = self.final_reduce_1(co_v1, co_v1)
            co_v2 = self.final_reduce_2(co_v2, co_v2, mask)
            co_v1 = l2norm(co_v1)
            co_v2 = l2norm(co_v2)
        else:
            co_v1 = self.reduce_func(co_v1, dim=-2)
            co_v2 = self.reduce_func(co_v2, dim=-2)
            co_v1 = l2norm(co_v1)
            co_v2 = l2norm(co_v2)

        if get_score:
            if self.fusion_func == "sum":
                 score = self.fc_out(co_v1 + co_v2).squeeze(dim=-1)
            elif self.fusion_func == "concat":
                 score = self.fc_out(torch.cat((co_v1, co_v2), dim=-1)).squeeze(dim=-1)
            if keep == "regions":
                score = score.transpose(0, 1)
            #mean_gate = gate_v1.mean(dim=-1).mean(dim=-1) + gate_v2.mean(dim=-1).mean(dim=-1)
            return score
        else:
            return torch.cat((co_v1, co_v2), dim=-1)
