import os
import sys
import logging
import numpy as np
import json
import re
import random
from tqdm import tqdm

sys.path.append(os.path.abspath(".."))

from basic.basic_task import Basic_task, Task_Mode
from basic.register import register_task, find_task
from utils.build_vocab import Vocab
from utils.utils import check_dir

import torch
from torch import nn
from transformers import BertPreTrainedModel, BertConfig, BertTokenizer, BertModel

from TorchCRF import CRF
from utils.ner_metrics import SeqEntityScore

logging.basicConfig(format='%(asctime)s:%(levelname)s: %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

workdir = os.getcwd()  # 当前路径
project_dir = os.path.split(workdir)[0]

"""
命名实体识别任务：
    模型：bert + BiLstm + CRF
    数据集：中文clue评测任务中cluener数据集：下载地址：https://www.cluebenchmarks.com/introduce.html
    infer部分代码还没实现
    开发集：acc: 0.7741 ,  recall: 0.7965 ,  f1: 0.7852  (bert-wwm-base)
"""

class Config:

    seed = 42   # 随机种子
    gpuids = "2"  # 设置显卡序号，若为None，则不使用gpu
    nlog = 50  # 多少step打印一次记录（loss，评估指标）
    early_stop = True

    train_batch_size = 32
    eval_batch_size = 32
    epochs = 5
    lr = 5e-5   # 学习率

    do_train = False
    do_eval = True
    do_infer = False

    # 新增超参数
    margin = 1
    max_len = 128
    rnn_dim = 128
    num_labels = 12
    use_lstm = True

    task_name = "Bert_LSTM_CRF"

    # 配置路径
    train_data_path = "/workspace/data/cluener/train.json"  # 训练集数据的路径，建议绝对路径
    dev_data_path = ["/workspace/data/cluener/dev.json"]  # 验证集数据的路径，建议绝对路径
    test_data_path = ["/workspace/data/cluener/test.json"]  # 测试集数据的路径，建议绝对路径

    # transformer结构(Bert, Albert, Roberta等)的预训练模型的配置, 路径也建议是绝对路径
    bert_model_path = "/workspace/Idiom_cloze/pretrained_models/chinese_wwm_pytorch/pytorch_model.bin"  # 预训练模型路径， 例如bert预训练模型
    model_config_path = "/workspace/Idiom_cloze/pretrained_models/chinese_wwm_pytorch/config.json"  # 预训练模型的config文件路径， 一般是json文件
    vocab_path = "/workspace/Idiom_cloze/pretrained_models/chinese_wwm_pytorch/vocab.txt"  # vocab文件路径，可以是预训练模型的vocab.txt文件

    model_save_path = project_dir + f"/model_save/{task_name.lower()}_model"  # 训练过程中最优模型或者训练结束后的模型保存路径
    output_path = project_dir + f"/output/{task_name.lower()}_model"  # 模型预测输出预测结果文件的路径

    # 新增文件路径
    label_list_path = "/workspace/data/cluener/label_list.txt"


# 构建模型动态计算图
class Model(BertPreTrainedModel):
    """
    模型说明：成语完形填空式阅读理解baseline模型
    """
    def __init__(self, model_config, task_config):
        super(Model, self).__init__(model_config)
        # 768 is the dimensionality of bert-base-uncased's hidden representations
        # Load the pretrained BERT model
        self.model_config = model_config
        self.task_config = task_config
        self.bert = BertModel(config=model_config)
        self.lstm = nn.LSTM(model_config.hidden_size, task_config.rnn_dim, num_layers=1, bidirectional=True, batch_first=True)
        self.dropout = nn.Dropout(0.5)
        self.linear = nn.Linear(task_config.rnn_dim * 2, task_config.num_labels)
        self.crf = CRF(task_config.num_labels, use_gpu=True)

        self.init_weights()

    def forward(self, inputs):

        input_ids = inputs.get("input_ids", None)
        attention_mask = inputs.get("input_masks", None)
        token_type_ids = inputs.get("token_type_ids", None)
        label_ids = inputs.get("label_ids", None)

        # input_ids [batch, max_seq_length]  sequence_outputs [batch, max_seq_length, hidden_state]
        bert_outputs = self.bert(input_ids, attention_mask, token_type_ids)
        sequence_outputs = bert_outputs[0]
        # blank_states = sequence_outputs[[i for i in range(len(positions))], positions]  # [batch, hidden_state]
        if self.task_config.use_lstm:
            sequence_outputs, _ = self.lstm(sequence_outputs)

        sequence_outputs_drop = self.dropout(sequence_outputs)
        emissions = self.linear(sequence_outputs_drop)
        
        logits = self.crf.viterbi_decode(emissions, attention_mask.byte())

        outputs = {
            "logits": logits,
        }
        if label_ids is not  None:
            loss = -1*self.crf(emissions, label_ids, mask=attention_mask.byte()) 
            outputs["loss"] = loss
        
        return outputs

# 编写任务
@ register_task
class Bert_LSTM_CRF(Basic_task):
    def __init__(self, task_config):
        super().__init__(task_config)
        self.task_config = task_config
        self.max_len = task_config.max_len
        
        # model init 模型初始化，加载预训练模型
        self.model_config = BertConfig.from_pretrained(self.task_config.model_config_path)
        # self.tokenizer = BertTokenizer.from_pretrained(self.task_config.vocab_path, lowercase=True)
        self.vocab = Vocab(task_config.vocab_path)
        self.label_vocab = Vocab(self.task_config.label_list_path)
        task_config.num_labels = self.label_vocab.vocab_size

        self.model = Model.from_pretrained(pretrained_model_name_or_path=self.task_config.bert_model_path,
                                           config=self.model_config, task_config=task_config)

        if self.task_config.gpuids != None:
            self.model.to(self.device)
        # 单机多卡训练
        if self.n_gpu > 1:
            self.model = nn.DataParallel(self.model)

    def evaluate(self, dataset, mode=Task_Mode.Eval, epoch=None):
        data_loader = torch.utils.data.DataLoader(
            dataset,
            shuffle=False,
            batch_size=self.task_config.eval_batch_size,
            num_workers=0
        )
        self.model.eval()
        outputs = []
        pred_labels = []
        true_labels = []
        metric = SeqEntityScore(self.label_vocab.id2word, markup="bio")
        loss_buffer = 0
        for bi, batch in enumerate(data_loader):
            model_outputs = self.run_one_step(batch, self.model)
            logits = model_outputs.pop("logits")      
            texts = batch["text"]
            label_ids = batch['label_ids'].detach().cpu().numpy().tolist()
            tags = logits
            for i, text in enumerate(texts):
                text_len = len(text)
                tag = tags[i][1:-1]
                label = label_ids[i][1:text_len + 1]
                assert len(tag) == text_len
                assert len(label) == text_len
                temp_1 = [self.label_vocab.id2word[l] for l in label]
                temp_2 = [self.label_vocab.id2word[t] for t in tag]
                metric.update(pred_paths=[temp_2], label_paths=[temp_1])
            
        if mode == Task_Mode.Eval:
            eval_info, entity_info = metric.result()
            info = ", ".join([f' {key}: {value:.4f} ' for key, value in eval_info.items()])
            logger.info(f"Evaluate: epoch={epoch}, step={self.global_step}, {info}")
            return eval_info["f1"]
        else:
            return pred_labels

    def train(self, dataset, valid_dataset=None):
        logging.info(f"train dataset size = {len(dataset)}")
        if valid_dataset is not None:
            logging.info(f"valid dataset size = {len(valid_dataset)}")
        data_loader = torch.utils.data.DataLoader(
            dataset,
            shuffle=True,
            batch_size=self.task_config.train_batch_size,
        )
        num_train_steps = int(len(dataset) / self.task_config.train_batch_size * self.task_config.epochs)
        optimizer, scheduler = self.create_optimizer(self.model, use_scheduler=True, num_warmup_steps=1000,
                                                     num_train_steps=num_train_steps)
        self.model.train()
        # Train the model on each batch
        # Reset gradients
        loss_buffer = 0
        for epoch in range(self.task_config.epochs):
            for bi, batch in enumerate(data_loader):
                self.model.zero_grad()
                outputs = self.run_one_step(batch, self.model)
                logits = outputs.pop("logits")
                loss = outputs.pop("loss")
                # Calculate gradients based on loss
                loss = loss.mean()
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()   #更新模型参数
                scheduler.step()  # 更新learning rate
                self.global_step += 1

                loss_buffer += loss.item()
                if self.global_step % self.task_config.nlog == 0:
                    logger.info("epoch={}, step={}, loss={:.4f}".format(epoch+1, self.global_step, loss_buffer / self.task_config.nlog))
                    loss_buffer = 0
                
            if valid_dataset != None:
                eval_score = self.evaluate(valid_dataset, mode=Task_Mode.Eval, epoch=epoch+1)
                self.model.train()
                if self.task_config.early_stop:
                    self.es(epoch, eval_score, self.model, model_path=self.task_config.model_save_path)
                    if self.es.early_stop:
                        logger.info("********** Early stopping ********")
                        break
            # 保存训练过程中的模型，防止意外程序停止，可以接着继续训练
            # self.save_checkpoint(model_path=self.task_config.model_save_path, epoch=epoch)
       
    
    def read_data(self, file, mode):
        """
        根据不同任务编写数据处理，建议将原始数据进行预处理之后再在这里写数据处理成模型输入结构
        """
        dataset = []
        with open(file, "r", encoding="utf-8") as fin:
            lines = fin.readlines()
            tk0 = tqdm(lines, total=len(lines))
            for line in tk0:
                line = json.loads(line)
                text = line["text"]

                input_ids = [self.vocab.get_id("[CLS]")] + [self.vocab.word2id.get(t, self.vocab.get_id("[UNK]")) for t in text] + [self.vocab.get_id("[SEP]")]
                token_type_ids = [0] * len(input_ids) + [0] * (self.max_len - len(input_ids))
                input_masks = [1] * len(input_ids) + [0] * (self.max_len - len(input_ids))

                if mode != Task_Mode.Infer:
                    label_entities = line.get('label', None)
                    words = list(text)
                    labels = ['O'] * len(words)
                    if label_entities is not None:
                        for key, value in label_entities.items():
                            for sub_name,sub_index in value.items():
                                for start_index,end_index in sub_index:
                                    assert  ''.join(words[start_index:end_index+1]) == sub_name
                                    if start_index == end_index:
                                        labels[start_index] = 'B-'+key
                                    else:
                                        labels[start_index] = 'B-'+key
                                        labels[start_index+1:end_index+1] = ['I-'+key]*(len(sub_name)-1)

                    label_ids = [0] + [self.label_vocab.word2id[each] for each in labels] + [0]
                    assert len(input_ids) == len(label_ids)
                    label_ids = label_ids + [0] * (self.max_len - len(label_ids))
                    assert len(label_ids) == self.max_len

                input_ids = input_ids + [0] * (self.max_len - len(input_ids))
                assert len(input_ids) == self.max_len
                assert len(input_masks) == self.max_len
                assert len(token_type_ids) == self.max_len
                if mode != Task_Mode.Infer:
                    dataset.append({
                        "text": text,
                        "labels": " ".join(labels),
                        'input_ids': torch.tensor(input_ids, dtype=torch.long),
                        'input_masks': torch.tensor(input_masks, dtype=torch.long),
                        'token_type_ids': torch.tensor(token_type_ids, dtype=torch.long),
                        'label_ids': torch.tensor(label_ids, dtype=torch.long),
                    })
                else:
                    dataset.append({
                        "text": text,
                        'input_ids': torch.tensor(input_ids, dtype=torch.long),
                        'input_masks': torch.tensor(input_masks, dtype=torch.long),
                        'token_type_ids': torch.tensor(token_type_ids, dtype=torch.long),
                    })

        return dataset


def seed_set(seed):
    '''
    set random seed of cpu and gpu
    '''
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

def run():
    config = Config()
    check_dir([config.model_save_path, config.output_path])
    seed_set(config.seed)
    os.environ["CUDA_VISIBLE_DEVICES"] = config.gpuids  # 设置gpu序号
    task_cls = find_task(config.task_name)
    task = task_cls(task_config=config)
    if config.do_train:
        dataset = task.read_data(config.train_data_path, mode=Task_Mode.Train)
        if config.do_eval:
            valid_dataset = task.read_data(config.dev_data_path[0], mode=Task_Mode.Eval)
            task.train(dataset, valid_dataset=valid_dataset)
        else:
            task.train(dataset)
    if config.do_eval:
        task.load_model(config.model_save_path)
        for dev_path in config.dev_data_path:
            logging.info(f"Evaluating model in {dev_path}")
            dataset = task.read_data(dev_path, mode=Task_Mode.Eval)
            logging.info(f"dev dataset size = {len(dataset)}")
            task.evaluate(dataset, mode=Task_Mode.Eval)
    if config.do_infer:
        task.load_model(config.model_save_path)
        for test_path in config.test_data_path:
            dataset = task.read_data(config.test_data_path, mode=Task_Mode.Infer)
            task.evaluate(dataset, mode=Task_Mode.Infer)

if __name__ == '__main__':
    run()
