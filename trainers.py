import torch
import time
import torcheval.metrics
import numpy as np
from datetime import datetime

from losses import NTXentLoss, SADLoss
from evals import SSDk, SAD, get_fpr


class EarlyStopping:
    def __init__(self, patience, min_delta):
        self.patience = patience
        self.min_delta = min_delta
        self.counter = 0
        self.early_stop = False

    def __call__(self, best_loss, current_loss):
        if current_loss <= (best_loss - self.min_delta):
            self.counter = 0
        else:
            self.counter += 1
            if self.counter >= self.patience:
                self.early_stop = True


class TrainerSSD:
    def __init__(self, model, train_loader, val_loader, criterion, config):
        self.model = model
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.criterion = criterion
        self.config = config
        self.ssd = None

    def train_epoch(self, optimizer):
        self.model.train()
        total_loss = 0.
        auprc = torcheval.metrics.BinaryAUPRC()
        f_lists = []
        y_lists = []

        for x, x_pos, y in self.train_loader:
            x, x_pos, y = x.to(self.config['device']), x_pos.to(self.config['device']), y.to(self.config['device'])
            optimizer.zero_grad()

            combined_x = torch.cat([x, x_pos], dim=0)
            batch_size = y.shape[0]

            features = self.model(combined_x)
            f1, f2 = torch.split(features, [batch_size, batch_size], dim=0)
            features = torch.cat([f1.unsqueeze(1), f2.unsqueeze(1)], dim=1)

            loss = self.criterion(features)
            loss.backward()
            optimizer.step()

            total_loss += loss.item()
            f_lists.append(f1)
            y_lists.append(y)

        f = torch.cat(f_lists, dim=0).detach().cpu().numpy()
        y = torch.cat(y_lists, dim=0).detach().cpu().numpy()

        avg_loss = total_loss/len(self.train_loader)

        self.ssd = SSDk(f, y)
        pred = self.ssd.get_score(f)
        auprc.update(torch.tensor(pred), torch.tensor(y))
        ap = auprc.compute().detach().cpu().numpy()
        auprc.reset()

        fpr = get_fpr(pred[y == 0], pred[y == 1])

        return avg_loss, ap, fpr

    def validate(self):
        with torch.no_grad():
            self.model.eval()
            total_loss = 0.
            auprc = torcheval.metrics.BinaryAUPRC()
            f_lists = []
            y_lists = []

            for x, x_pos, y in self.val_loader:
                x, x_pos, y = x.to(self.config['device']), x_pos.to(self.config['device']), y.to(self.config['device'])

                combined_x = torch.cat([x, x_pos], dim=0)
                batch_size = y.shape[0]
                features = self.model(combined_x)

                f1, f2 = torch.split(features, [batch_size, batch_size], dim=0)
                features = torch.cat([f1.unsqueeze(1), f2.unsqueeze(1)], dim=1)

                loss = self.criterion(features)
                total_loss += loss.item()
                f_lists.append(f1)
                y_lists.append(y)

            f = torch.cat(f_lists, dim=0).detach().cpu().numpy()
            y = torch.cat(y_lists, dim=0).detach().cpu().numpy()

            avg_loss = total_loss/len(self.val_loader)

            pred = self.ssd.get_score(f)
            auprc.update(torch.tensor(pred), torch.tensor(y))
            ap = auprc.compute().detach().cpu().numpy()
            auprc.reset()

            fpr = get_fpr(pred[y == 0], pred[y == 1])

        return avg_loss, ap, fpr


class TrainerSAD:
    def __init__(self, model, train_loader, val_loader, criterion, config):
        self.model = model
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.criterion = criterion
        self.config = config
        self.method = config['method']
        self.c = None
        self.cov = None
        self.sad = None

    def train_epoch(self, optimizer):
        self.model.train()
        total_loss = 0.
        auprc = torcheval.metrics.BinaryAUPRC()
        f_lists = []
        y_lists = []

        for x, y in self.train_loader:
            x, y = x.to(self.config['device']), y.to(self.config['device'])
            optimizer.zero_grad()
            features = self.model(x)

            if self.c is None:
                known = features[y == 0]
                c = torch.mean(known, dim=0)
                c[(abs(c) < 1e-6) & (c < 0)] = -1e-6
                c[(abs(c) < 1e-6) & (c > 0)] = 1e-6
                self.c = c.detach()
                self.cov = torch.cov(known.detach().mT)


            loss = self.criterion(features, y, self.c, self.cov)
            loss.backward()
            optimizer.step()

            total_loss += loss.item()
            f_lists.append(features)
            y_lists.append(y)

        f = torch.cat(f_lists, dim=0).detach().cpu().numpy()
        y = torch.cat(y_lists, dim=0).detach().cpu().numpy()

        avg_loss = total_loss/len(self.train_loader)

        self.sad = SAD(f, y)
        if self.method == 'sad':
            pred = self.sad.get_score(z=f, c=self.c.cpu().numpy())
        else:
            pred = self.sad.get_mahalanobis_score(z=f)
        auprc.update(torch.tensor(pred), torch.tensor(y))
        ap = auprc.compute().detach().cpu().numpy()
        auprc.reset()

        fpr = get_fpr(pred[y == 0], pred[y == 1])

        return avg_loss, ap, fpr

    def validate(self):
        with torch.no_grad():
            self.model.eval()
            total_loss = 0.
            auprc = torcheval.metrics.BinaryAUPRC()
            f_lists = []
            y_lists = []

            for x, y in self.val_loader:
                x, y = x.to(self.config['device']), y.to(self.config['device'])
                features = self.model(x)
                loss = self.criterion(features, y, self.c, self.cov)

                total_loss += loss.item()
                f_lists.append(features)
                y_lists.append(y)

            f = torch.cat(f_lists, dim=0).detach().cpu().numpy()
            y = torch.cat(y_lists, dim=0).detach().cpu().numpy()

            avg_loss = total_loss/len(self.val_loader)

            if self.method == 'sad':
                pred = self.sad.get_score(z=f, c=self.c)
            else:
                pred = self.sad.get_mahalanobis_score(z=f)
            auprc.update(torch.tensor(pred), torch.tensor(y))
            ap = auprc.compute().detach().cpu().numpy()
            auprc.reset()

            fpr = get_fpr(pred[y == 0], pred[y == 1])

        return avg_loss, ap, fpr


def train(model, train_loader, val_loader, config):
    torch.manual_seed(config['seed'])
    torch.cuda.manual_seed(config['seed'])
    torch.cuda.manual_seed_all(config['seed'])
    np.random.seed(config['seed'])

    if config['method'] == 'ssd':
        criterion = NTXentLoss(temperature=config['temperature'], contrast_mode=config['contrast_mode'])
        trainer = TrainerSSD(model, train_loader, val_loader, criterion, config)
        hyperparam_str = str(config['temperature']) + 'temp_'
    else:
        criterion = SADLoss(eta=config['eta'], method=config['method'])
        trainer = TrainerSAD(model, train_loader, val_loader, criterion, config)
        hyperparam_str = str(config['eta']) + 'eta_'

    optimizer = torch.optim.Adam(model.parameters(),
                                 lr=config['lr'],
                                 weight_decay=config['weight_decay'],
                                 )

    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer,
                                                           factor=config['sched_factor'],
                                                           patience=config['sched_patience'],
                                                           )

    early_stopping = EarlyStopping(config['patience'], config['min_delta'])

    train_losses = []
    val_losses = []
    train_ap = []
    val_ap = []
    train_fpr = []
    val_fpr = []

    start_time = time.time()
    best_model_wts = model.state_dict()
    best_loss = 1e12
    best_epoch = 0

    for epoch in range(1, config['epochs']+1):
        epoch_loss, epoch_ap, epoch_fpr = trainer.train_epoch(optimizer)
        epoch_val_loss, epoch_val_ap, epoch_val_fpr = trainer.validate()

        train_losses.append(epoch_loss)
        val_losses.append(epoch_val_loss)
        train_ap.append(epoch_ap)
        val_ap.append(epoch_val_ap)
        train_fpr.append(epoch_fpr)
        val_fpr.append(epoch_val_fpr)

        if epoch % config['print_freq'] == 0 or epoch == 1:
            print(f"Epoch {epoch} | "
                  f"Train Loss: {epoch_loss:.4f} | "
                  f"Val Loss: {epoch_val_loss:.4f} | "
                  f"Train AUPRC: {epoch_ap:.4f} | "
                  f"Val AUPRC: {epoch_val_ap:.4f} | "
                  f"Train FPR@95TPR: {epoch_fpr:.4f} | "
                  f"Val FPR@95TPR: {epoch_val_fpr:.4f} | "
                  f"Best Epoch: {best_epoch}")

        early_stopping(best_loss, epoch_val_loss)
        if early_stopping.early_stop:
            print("Early stopping")
            print(f"Epoch {epoch} | "
                  f"Train Loss: {epoch_loss:.4f} | "
                  f"Val Loss: {epoch_val_loss:.4f} | "
                  f"Train AUPRC: {epoch_ap:.4f} | "
                  f"Val AUPRC: {epoch_val_ap:.4f} | "
                  f"Train FPR@95TPR: {epoch_fpr:.4f} | "
                  f"Val FPR@95TPR: {epoch_val_fpr:.4f} | "
                  f"Best Epoch: {best_epoch}")
            break

        if epoch_val_loss < best_loss:
            best_loss = epoch_val_loss
            best_epoch = epoch
            best_model_wts = model.state_dict()

        scheduler.step(epoch_val_loss)

    duration = time.time() - start_time
    print('Training completed in {:.0f}m {:.0f}s'.format(duration // 60, duration % 60))

    # save best model
    model.load_state_dict(best_model_wts)
    save_dir = "./Experiments/"
    stamp = datetime.today().strftime('%Y%m%d_%H%M')
    model_out_path = save_dir + str(config['method']) + '_' + hyperparam_str + stamp + '.pth'
    torch.save(model, model_out_path)
    return model, train_losses, val_losses, train_ap, val_ap, train_fpr, val_fpr
