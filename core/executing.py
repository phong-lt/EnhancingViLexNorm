import os
import json
import torch
from torch.utils.data import DataLoader

from logger.logger import Logger

from .dataset import LexDataset
from .modeling import LexBARTModel, LexT5Model

from timeit import default_timer as timer
from tqdm import tqdm

from evaluation.err import compute_err_metrics

from transformers import AutoTokenizer



class Executor():
    def __init__(self, config, mode = 'train', evaltype='last', predicttype='best'):
        print("---Initializing Executor---")
        self.mode = mode
        self.config = config
        self.evaltype = evaltype
        self.predicttype = predicttype

        self.best_score = 0

        if self.mode == "train":
            self._create_data_utils()       

            if self.config.modeltype == "t5":
                self.model = LexT5Model(self.config.pretrained_name)
            else:
                self.model = LexBARTModel(self.config.pretrained_name)

            self.model = self.model.to(self.config.DEVICE)

            self.optim = torch.optim.Adam(self.model.parameters(), lr=config.LR, betas=config.BETAS, eps=1e-9)

            self.loss_fn = torch.nn.CrossEntropyLoss(ignore_index=self.tokenizer.pad_token_id)
            
            self.scheduler = torch.optim.lr_scheduler.LinearLR(optimizer = self.optim, total_iters = config.warmup_step)

            self.SAVE = config.SAVE
            self._create_dataloader()

            if os.path.isfile(os.path.join(self.config.SAVE_PATH, "last_ckp.pth")):
                print("###Load trained checkpoint ...")
                ckp = torch.load(os.path.join(self.config.SAVE_PATH, "last_ckp.pth"))
                try:
                    print(f"\t- Last train epoch: {ckp['epoch']}")
                except:
                    print(f"\t- Last train step: {ckp['step']}")
                self.model.load_state_dict(ckp['state_dict'])
                self.optim.load_state_dict(ckp['optimizer'])
                self.scheduler.load_state_dict(ckp['scheduler'])
                self.best_score = ckp['best_score']
            
        if self.mode in ["eval", "predict"]:
            self.init_eval_predict_mode()

           
            if self.config.modeltype == "t5":
                self.model = LexT5Model(self.config.pretrained_name)
            else:
                self.model = LexBARTModel(self.config.pretrained_name)

            self.model = self.model.to(self.config.DEVICE)
    
    def run(self):
        log = Logger("./terminal.txt")
        log.start()

        if self.mode =='train':
            if self.config.DO_PRETRAINING:
                self._pretrain_step()
            self._train_step()
        elif self.mode == 'eval':
            self.evaluate()
        elif self.mode == 'predict':
            self.predict()
        else:
            exit(-1)

        log.stop()


    def evaluate(self):
        print("###Evaluate Mode###")

        if os.path.isfile(os.path.join(self.config.SAVE_PATH, f"{self.evaltype}_ckp.pth")):
            print("###Load trained checkpoint ...")
            ckp = torch.load(os.path.join(self.config.SAVE_PATH, f"{self.evaltype}_ckp.pth"))
            try:
                print(f"\t- Using {self.evaltype} train epoch: {ckp['epoch']}")
            except:
                print(f"\t- Using {self.evaltype} train step: {ckp['step']}")
            self.model.load_state_dict(ckp['state_dict'])

        elif os.path.isfile(os.path.join('./models', f"{self.evaltype}_ckp.pth")):
            print("###Load trained checkpoint ...")
            ckp = torch.load(os.path.join('./models', f"{self.evaltype}_ckp.pth"))
            try:
                print(f"\t- Using {self.evaltype} train epoch: {ckp['epoch']}")
            except:
                print(f"\t- Using {self.evaltype} train step: {ckp['step']}")
            self.model.load_state_dict(ckp['state_dict'])
        
        else:
            print(f"(!) {self.evaltype}_ckp.pth is required (!)")
            return 
        
        with torch.no_grad():
            print(f'Evaluate val data ...')

            res = self._evaluate_metrics()
            print(res)
    
    def predict(self): 
        print("###Predict Mode###")
        if os.path.isfile(os.path.join(self.config.SAVE_PATH, f"{self.predicttype}_ckp.pth")):
            print("###Load trained checkpoint ...")
            ckp = torch.load(os.path.join(self.config.SAVE_PATH, f"{self.predicttype}_ckp.pth"))
            try:
                print(f"\t- Using {self.predicttype} train epoch: {ckp['epoch']}")
            except:
                print(f"\t- Using {self.predicttype} train step: {ckp['step']}")
            self.model.load_state_dict(ckp['state_dict'])

        elif os.path.isfile(os.path.join('./models', f"{self.predicttype}_ckp.pth")):
            print("###Load trained checkpoint ...")
            ckp = torch.load(os.path.join('./models', f"{self.predicttype}_ckp.pth"))
            try:
                print(f"\t- Using {self.predicttype} train epoch: {ckp['epoch']}")
            except:
                print(f"\t- Using {self.predicttype} train step: {ckp['step']}")
            self.model.load_state_dict(ckp['state_dict'])
        else:
            print(f"(!) {self.predicttype}_ckp.pth is required  (!)")
            return

        print("## START PREDICTING ... ")

        if self.config.get_predict_score:
            results, scores = self._evaluate_metrics()
            print(f'\t#PREDICTION:\n')
            print(f'\t{scores}')
        else:
            preds = self.infer(self.predictiter, self.config.max_predict_length)
            results = [{"pred": p} for p in preds]


        if self.config.SAVE_PATH:
            with open(os.path.join(self.config.SAVE_PATH, "results.json"), 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=4)
            print("Saved Results !")
        else:
            with open(os.path.join(".","results.csv"), 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=4)
            print("Saved Results !")
            
    def _create_data_utils(self):
        
        self.tokenizer = AutoTokenizer.from_pretrained(self.config.pretrained_name)


        print("# Creating Datasets")

        if self.config.DO_PRETRAINING:
            self.pretrain_data = LexDataset(data_path = self.config.pretrain_data_path,
                                            tokenizer = self.tokenizer,
                                            modeltype = self.config.modeltype,
                                            batch = 256,
                                            src_max_token_len = self.config.src_max_token_len,
                                            trg_max_token_len = self.config.trg_max_token_len)
        
        self.train_data = LexDataset(data_path = self.config.train_path,
                                            tokenizer = self.tokenizer,
                                            modeltype = self.config.modeltype,
                                            batch = 256,
                                            src_max_token_len = self.config.src_max_token_len,
                                            trg_max_token_len = self.config.trg_max_token_len)
        self.val_data = LexDataset(data_path = self.config.val_path,
                                            tokenizer = self.tokenizer,
                                            modeltype = self.config.modeltype,
                                            batch = 256,
                                            src_max_token_len = self.config.src_max_token_len,
                                            trg_max_token_len = self.config.trg_max_token_len)
    

    def _create_dataloader(self):
        print("# Creating DataLoaders")

        if self.config.DO_PRETRAINING:
            self.pretrainiter = DataLoader(dataset = self.pretrain_data, 
                                    batch_size=self.config.PRETRAIN_BATCH_SIZE, 
                                    shuffle=True)
       
        self.trainiter = DataLoader(dataset = self.train_data, 
                                    batch_size=self.config.TRAIN_BATCH_SIZE, 
                                    shuffle=True)
        self.valiter = DataLoader(dataset = self.val_data, 
                                    batch_size=self.config.EVAL_BATCH_SIZE)

    def init_eval_predict_mode(self):
        self.tokenizer = AutoTokenizer.from_pretrained(self.config.pretrained_name)

        if self.mode == "eval":
            print("###Load eval data ...")
            self.val_data = LexDataset(data_path = self.config.val_path,
                                            tokenizer = self.tokenizer,
                                            modeltype = self.config.modeltype,
                                            batch = 256,
                                            src_max_token_len = self.config.src_max_token_len,
                                            trg_max_token_len = self.config.trg_max_token_len)
            
            self.valiter = DataLoader(dataset = self.val_data, 
                                    batch_size=self.config.EVAL_BATCH_SIZE)

        elif self.mode == "predict":
            print("###Load predict data ...")
            self.predict_data = LexDataset(data_path = self.config.predict_path,
                                            tokenizer = self.tokenizer,
                                            modeltype = self.config.modeltype,
                                            batch = 256,
                                            src_max_token_len = self.config.src_max_token_len,
                                            trg_max_token_len = self.config.trg_max_token_len)
            

            self.predictiter = DataLoader(dataset = self.predict_data, 
                                    batch_size=self.config.PREDICT_BATCH_SIZE)

    
    def _evaluate(self):
        self.model.eval()
        losses = 0
        with tqdm(desc='Validating... ' , unit='it', total=len(list(self.valiter))) as pbar:
            with torch.no_grad():
                for it, batch in enumerate(self.valiter):
                    label_attention_mask = batch['label_attention_mask'].to(self.config.DEVICE)
                    labels = batch['labels'].type(torch.long).to(self.config.DEVICE)

                    trg_input = labels[:, :-1]
                    label_attention_mask = label_attention_mask[:, :-1]

                    logits = self.model(input_ids = batch['input_ids'].to(self.config.DEVICE),
                                        label_ids = trg_input,
                                        src_attention_mask = batch['src_attention_mask'].to(self.config.DEVICE),
                                        label_attention_mask = label_attention_mask)

                    trg_out = labels[:, 1:]

                    loss = self.loss_fn(logits.reshape(-1, logits.shape[-1]), trg_out.reshape(-1))
                    losses += loss.data.item()

                    pbar.set_postfix(loss=losses / (it + 1))
                    pbar.update()


        return losses / len(list(self.valiter))
    
    def _pretrain_step(self):
        assert self.config.NUM_PRETRAIN_STEP is not None
        assert self.config.NUM_PRETRAIN_STEP > 0

        if not self.config.SAVE_PATH:
            folder = './models'
        else:
            folder = self.config.SAVE_PATH
        
        if not os.path.exists(folder):
            os.mkdir(folder)

        self.model.train()

        losses = 0
        current_step = 0

        print(f"#----------- START PRE-TRAINING -----------------#")
        print(f"(!) Show pre-train loss after each {self.config.show_loss_after_pretrain_steps} steps")
        print(f"(!) Save model after each {self.config.save_after_pretrain_steps} steps")
        s_train_time = timer()

        while True:
            for batch in self.pretrainiter:
                label_attention_mask = batch['label_attention_mask'].to(self.config.DEVICE)
                labels = batch['labels'].type(torch.long).to(self.config.DEVICE)


                trg_input = labels[:, :-1]
                label_attention_mask = label_attention_mask[:, :-1]

                logits = self.model(input_ids = batch['input_ids'].to(self.config.DEVICE),
                                    label_ids = trg_input,
                                    src_attention_mask = batch['src_attention_mask'].to(self.config.DEVICE),
                                    label_attention_mask = label_attention_mask)


                self.optim.zero_grad()

                trg_out = labels[:, 1:]

                loss = self.loss_fn(logits.reshape(-1, logits.shape[-1]), trg_out.reshape(-1))
                loss.backward()

                self.optim.step()

                self.scheduler.step()
                
                losses += loss.data.item()

                current_step += 1

                if current_step % self.config.show_loss_after_pretrain_steps == 0:
                    print(f"[Step {current_step} | {int(current_step/self.config.NUM_PRETRAIN_STEP*100)}% completed] Train Loss: {losses / current_step}")

                if current_step % self.config.save_after_pretrain_steps == 0:
                    if self.SAVE:
                        lstatedict = {
                                    "state_dict": self.model.state_dict(),
                                    "optimizer": self.optim.state_dict(),
                                    "scheduler": self.scheduler.state_dict(),
                                    "step": current_step,
                                    "best_score": self.best_score
                                }

                        lfilename = f"last_ckp.pth"
                        torch.save(lstatedict, os.path.join(folder,lfilename))

                if current_step >= self.config.NUM_PRETRAIN_STEP:   
                    e_train_time = timer()
                    print(f"#----------- PRE-TRAINING END-Time: { e_train_time-s_train_time} -----------------#")
                    return


    def _train_step(self):
        assert self.config.NUM_TRAIN_STEP is not None
        assert self.config.NUM_TRAIN_STEP > 0

        if not self.config.SAVE_PATH:
            folder = './models'
        else:
            folder = self.config.SAVE_PATH
        
        if not os.path.exists(folder):
            os.mkdir(folder)

        self.model.train()

        losses = 0
        current_step = 0

        m_err = 0
        m_step = 0

        print(f"#----------- START TRAINING -----------------#")
        print(f"(!) Show train loss after each {self.config.show_loss_after_steps} steps")
        print(f"(!) Evaluate after each {self.config.eval_after_steps} steps")
        s_train_time = timer()

        while True:
            for batch in self.trainiter:
                label_attention_mask = batch['label_attention_mask'].to(self.config.DEVICE)
                labels = batch['labels'].type(torch.long).to(self.config.DEVICE)


                trg_input = labels[:, :-1]
                label_attention_mask = label_attention_mask[:, :-1]

                logits = self.model(input_ids = batch['input_ids'].to(self.config.DEVICE),
                                    label_ids = trg_input,
                                    src_attention_mask = batch['src_attention_mask'].to(self.config.DEVICE),
                                    label_attention_mask = label_attention_mask)


                self.optim.zero_grad()

                trg_out = labels[:, 1:]

                loss = self.loss_fn(logits.reshape(-1, logits.shape[-1]), trg_out.reshape(-1))
                loss.backward()

                self.optim.step()

                self.scheduler.step()
                
                losses += loss.data.item()

                current_step += 1

                if current_step % self.config.show_loss_after_steps == 0:
                    print(f"[Step {current_step} | {int(current_step/self.config.NUM_TRAIN_STEP*100)}% completed] Train Loss: {losses / current_step}")

                if current_step % self.config.eval_after_steps == 0:
                    eval_loss = self._evaluate()
                    res = self._evaluate_metrics()
                    err = res["ERR"]
                    print(f'\tTraining Step {current_step}:')
                    print(f'\tTrain Loss: {losses / current_step} - Val. Loss: {eval_loss:.4f}')
                    print(res)
                    
                    if m_err < err:
                        m_err = err
                        m_step = current_step

                    if self.SAVE:
                        if self.best_score < err:
                            self.best_score = err
                            statedict = {
                                "state_dict": self.model.state_dict(),
                                "optimizer": self.optim.state_dict(),
                                "scheduler": self.scheduler.state_dict(),
                                "step": current_step,
                                "best_score": self.best_score
                            }

                            filename = f"best_ckp.pth"
                            torch.save(statedict, os.path.join(folder,filename))
                            print(f"!---------Saved {filename}----------!")

                        lstatedict = {
                                    "state_dict": self.model.state_dict(),
                                    "optimizer": self.optim.state_dict(),
                                    "scheduler": self.scheduler.state_dict(),
                                    "step": current_step,
                                    "best_score": self.best_score
                                }

                        lfilename = f"last_ckp.pth"
                        torch.save(lstatedict, os.path.join(folder,lfilename))

                if current_step >= self.config.NUM_TRAIN_STEP:
                    if m_err < self.best_score:
                        m_err = self.best_score
                        m_step = -1
                    e_train_time = timer()
                    print(f"\n# BEST RESULT:\n\tStep: {m_step}\n\tBest F1: {m_err:.4f}")
                    print(f"#----------- TRAINING END-Time: { e_train_time-s_train_time} -----------------#")
                    return
    
        
    
    def infer_post_processing(self, out_ids):
        res = []
        for out in out_ids:
            try:
                res.append(out[1:out.index(self.tokenizer.eos_token_id)])
            except:
                res.append(out)

        return res

    def infer(self, dataloader, max_length):
        self.model.eval()

        decoded_preds = []

        with tqdm(desc='Inferring... ', unit='it', total=len(list(dataloader))) as pbar:
            with torch.no_grad():
                for batch in dataloader:
                   
                    pred = self.model.generate( input_ids = batch['input_ids'].to(self.config.DEVICE),
                                                max_length = max_length)

                    decoded_preds += self.tokenizer.batch_decode(self.infer_post_processing(pred.tolist()), skip_special_tokens=True)

                    pbar.update()

        return decoded_preds


    def _evaluate_metrics(self):
        if self.mode == "predict":
            preds = self.infer(self.predictiter, self.config.max_predict_length)
            gts = [i.strip() for i in self.predict_data.trg]
            raw_srcs = [i.strip() for i in self.predict_data.src]
        else:
            preds = self.infer(self.valiter, self.config.max_eval_length)
            gts = [i.strip() for i in self.val_data.trg]
            raw_srcs = [i.strip() for i in self.val_data.src]

        preds = [i.strip() for i in preds]

        if self.mode == "predict":
            result = [{
                "pred": pred,
                "gt": gt,
                "raw_src": raw_src
            } for pred, gt, raw_src in zip(preds, gts, raw_srcs)]

            return result, compute_err_metrics(raw_srcs, gts, preds)

        return compute_err_metrics(raw_srcs, gts, preds)

        
        


  