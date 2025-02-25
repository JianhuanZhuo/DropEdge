from __future__ import division
from __future__ import print_function

import argparse
import time

import numpy as np
import torch.optim as optim
import wandb
from tqdm import tqdm

from earlystopping import EarlyStopping
from metric import accuracy
from metric import roc_auc_compute_fn
from models import *
from sample import Sampler

# Training settings
parser = argparse.ArgumentParser()
# Training parameter 
parser.add_argument('--no_cuda', action='store_true', default=False, help='Disables CUDA training.')
parser.add_argument("--mixmode", action="store_true", default=False, help="Enable CPU GPU mixing mode.")
parser.add_argument('--lradjust', action='store_true', default=False, help='(ReduceLROnPlateau or Linear Reduce)')
parser.add_argument('--epochs', type=int, default=400, help='Number of epochs to train.')
parser.add_argument('--lr', type=float, default=0.01, help='Initial learning rate.')
parser.add_argument('--weight_decay', type=float, default=0.005, help='Weight decay (L2 loss on parameters).')
parser.add_argument("--warm_start", default="", help="The model name to be loaded for warm start.")
parser.add_argument('--dataset', default="cora", help="The data set")
parser.add_argument('--datapath', default="data/", help="The data path.")
parser.add_argument("--early_stopping", type=int, default=400, help="The patience of earlystopping. Do not when 0.")
parser.add_argument('--seed', type=int, default=42, help='Random seed.')
parser.add_argument('--worker', type=bool, default=False)

# Model parameter
parser.add_argument('--type', default='mutigcn', help="(mutigcn, resgcn, densegcn, inceptiongcn)")
parser.add_argument('--inputlayer', default='gcn', help="The input layer of the model.")
parser.add_argument('--outputlayer', default='gcn', help="The output layer of the model.")
parser.add_argument('--hidden', type=int, default=128,   help='Number of hidden units.')
parser.add_argument('--dropout', type=float, default=0.8,  help='Dropout rate (1 - keep probability).')
parser.add_argument('--withbn', action='store_true', default=False,  help='Enable Bath Norm GCN')
parser.add_argument('--withloop', action="store_true", default=False, help="Enable loop layer GCN")
parser.add_argument('--nhiddenlayer', type=int, default=1, help='The number of hidden layers.')
parser.add_argument("--normalization", default="FirstOrderGCN", help="The normalization on the adj matrix.")
parser.add_argument("--sampling_percent", type=float, default=0.7, help="The percent. If 1, no sampling")
parser.add_argument("--nbaseblocklayer", type=int, default=2,  help="The number of layers in each baseblock")
parser.add_argument("--aggrmethod", default="default", help="The aggrmethod for the layer aggreation. "
                                                            "The options includes add and concat. "
                                                            "Only valid in resgcn, densegcn and inecptiongcn")
parser.add_argument("--task_type", default="full", help="The node classification task type (full and semi). "
                                                        "Only valid for cora, citeseer and pubmed dataset.")

args = parser.parse_args()

if args.worker:
    run = wandb.init()
    for k, v in run.config.items():
        setattr(args, k, v)
    names = '-'.join([
        f'{k}{v}'
        for k, v in sorted(run.config.items())
    ])
    run.name = f"run-{names}"
else:
    run = wandb.init(
        project='CAGCN-DropEdge-test',
        name='DropEdge-test',
        allow_val_change=True)

print("-="*40)
print(args)
print("-="*40)

# pre setting
args.cuda = not args.no_cuda and torch.cuda.is_available()
args.mixmode = args.no_cuda and args.mixmode and torch.cuda.is_available()
if args.aggrmethod == "default":
    if args.type == "resgcn":
        args.aggrmethod = "add"
    else:
        args.aggrmethod = "concat"

if args.type == "mutigcn":
    print("For the multi-layer gcn model, the aggrmethod is fixed to nores and nhiddenlayers = 1.")
    args.nhiddenlayer = 1
    args.aggrmethod = "nores"

# random seed setting
np.random.seed(args.seed)
torch.manual_seed(args.seed)
if args.cuda or args.mixmode:
    torch.cuda.manual_seed(args.seed)

# should we need fix random seed here?
sampler = Sampler(args.dataset, args.datapath, args.task_type)

# get labels and indexes
labels, idx_train, idx_val, idx_test = sampler.get_label_and_idxes(args.cuda)
nfeat = sampler.nfeat
nclass = sampler.nclass
print("nclass: %d\tnfea:%d" % (nclass, nfeat))

# The model
model = GCNModel(nfeat=nfeat,
                 nhid=args.hidden,
                 nclass=nclass,
                 nhidlayer=args.nhiddenlayer,
                 dropout=args.dropout,
                 baseblock=args.type,
                 inputlayer=args.inputlayer,
                 outputlayer=args.outputlayer,
                 nbaselayer=args.nbaseblocklayer,
                 activation=F.relu,
                 withbn=args.withbn,
                 withloop=args.withloop,
                 aggrmethod=args.aggrmethod,
                 mixmode=args.mixmode)

optimizer = optim.Adam(model.parameters(),
                       lr=args.lr, weight_decay=args.weight_decay)

scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=[200, 300, 400, 500, 600, 700], gamma=0.5)
# convert to cuda
if args.cuda:
    model.cuda()

# For the mix mode, lables and indexes are in cuda. 
if args.cuda or args.mixmode:
    labels = labels.cuda()
    idx_train = idx_train.cuda()
    idx_val = idx_val.cuda()
    idx_test = idx_test.cuda()

if args.warm_start is not None and args.warm_start != "":
    early_stopping = EarlyStopping(fname=args.warm_start, verbose=False)
    print("Restore checkpoint from %s" % (early_stopping.fname))
    model.load_state_dict(early_stopping.load_checkpoint())

# set early_stopping
if args.early_stopping > 0:
    early_stopping = EarlyStopping(patience=args.early_stopping, verbose=False)
    print("Model is saving to: %s" % (early_stopping.fname))

def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group['lr']

# define the training function.
def train(epoch, train_adj, train_fea, idx_train, val_adj=None, val_fea=None):
    if val_adj is None:
        val_adj = train_adj
        val_fea = train_fea

    t = time.time()
    model.train()
    optimizer.zero_grad()
    output = model(train_fea, train_adj)
    # special for reddit
    if sampler.learning_type == "inductive":
        loss_train = F.nll_loss(output, labels[idx_train])
        acc_train = accuracy(output, labels[idx_train])
    else:
        loss_train = F.nll_loss(output[idx_train], labels[idx_train])
        acc_train = accuracy(output[idx_train], labels[idx_train])

    loss_train.backward()
    optimizer.step()
    train_t = time.time() - t
    val_t = time.time()
    if args.early_stopping > 0 and sampler.dataset != "reddit":
        loss_val = F.nll_loss(output[idx_val], labels[idx_val]).item()
        early_stopping(loss_val, model)

    model.eval()
    output = model(val_fea, val_adj)
    loss_val = F.nll_loss(output[idx_val], labels[idx_val]).item()
    acc_val = accuracy(output[idx_val], labels[idx_val]).item()
    if sampler.dataset == "reddit":
        early_stopping(loss_val, model)

    if args.lradjust:
        scheduler.step()

    val_t = time.time() - val_t
    return loss_train.item(), acc_train.item(), loss_val, acc_val, get_lr(optimizer), train_t, val_t


def test(test_adj, test_fea):
    model.eval()
    output = model(test_fea, test_adj)
    loss_test = F.nll_loss(output[idx_test], labels[idx_test])
    acc_test = accuracy(output[idx_test], labels[idx_test])
    auc_test = roc_auc_compute_fn(output[idx_test], labels[idx_test])
    print("Test set results:",
          "loss= {:.4f}".format(loss_test.item()),
          "auc= {:.4f}".format(auc_test),
          "accuracy= {:.4f}".format(acc_test.item()))
    print("accuracy=%.5f" % (acc_test.item()))
    return loss_test.item(), acc_test.item()

# Train model
t_total    = time.time()
loss_train = np.zeros((args.epochs,))
acc_train  = np.zeros((args.epochs,))
loss_val   = np.zeros((args.epochs,))
acc_val    = np.zeros((args.epochs,))

sampling_t = 0

for epoch in tqdm(range(args.epochs), desc="epoch"):
    input_idx_train = idx_train
    sampling_t = time.time()
    # no sampling
    # randomedge sampling if args.sampling_percent >= 1.0, it behaves the same as stub_sampler.
    (train_adj, train_fea) = sampler.randomedge_sampler(percent=args.sampling_percent, normalization=args.normalization,
                                                        cuda=args.cuda)
    if args.mixmode:
        train_adj = train_adj.cuda()

    sampling_t = time.time() - sampling_t
    
    (val_adj, val_fea) = sampler.get_test_set(normalization=args.normalization, cuda=args.cuda)
    if args.mixmode:
        val_adj = val_adj.cuda()
    outputs = train(epoch, train_adj, train_fea, input_idx_train, val_adj, val_fea)

    run.log({
        'loss_train': outputs[0],
        'accs_train': outputs[1],
        'loss_val':   outputs[2],
        'accs_val':   outputs[3],
        'lr':         outputs[4],
        'time_train': outputs[5],
        'time_val':   outputs[6],
    }, epoch)

    loss_train[epoch], acc_train[epoch], loss_val[epoch], acc_val[epoch] = outputs[0], outputs[1], outputs[2], outputs[3]

    if args.early_stopping > 0 and early_stopping.early_stop:
        print("Early stopping.")
        model.load_state_dict(early_stopping.load_checkpoint())
        break

if args.early_stopping > 0:
    model.load_state_dict(early_stopping.load_checkpoint())


# Testing
(test_adj, test_fea) = sampler.get_test_set(normalization=args.normalization, cuda=args.cuda)
if args.mixmode:
    test_adj = test_adj.cuda()
(loss_test, acc_test) = test(test_adj, test_fea)
print("%.6f\t%.6f\t%.6f\t%.6f\t%.6f\t%.6f" % (loss_train[-1], loss_val[-1], loss_test,
                                              acc_train[-1], acc_val[-1], acc_test))
run.log({
    'test/loss_train': loss_train[-1],
    'test/loss_val': loss_val[-1],
    'test/loss_test': loss_test,
    'test/acc_train': acc_train[-1],
    'test/acc_val': acc_val[-1],
    'test/acc_test': acc_test,
})


