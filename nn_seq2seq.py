#!/usr/bin/env python
import argparse
from bidirectional_gru_rnn import BidirectionalGruRnn
from concat_with_softmax import ConcatWithSoftmax
from gru_rnn import GruRnn
import itertools
import json
import numpy as np
import os
import random
from simple_rnn import SimpleRnn
from sklearn.metrics import confusion_matrix
from stats import Stats
import sys
import time
import theano
import theano.tensor as T
import util
from updates import *
from vocab import Vocab

parser = argparse.ArgumentParser()
parser.add_argument("--train-set", default="data/snli_1.0_train.jsonl")
parser.add_argument("--num-from-train", default=-1, type=int,
                    help='number of egs to read from train. -1 => all')
parser.add_argument("--dev-set", default="data/snli_1.0_dev.jsonl")
parser.add_argument("--num-from-dev", default=-1, type=int,
                    help='number of egs to read from dev. -1 => all')
parser.add_argument("--dev-run-freq", default=100000, type=int,
                    help='frequency (in num examples trained) to run against dev set')
parser.add_argument("--num-epochs", default=-1, type=int,
                    help='number of epoches to run. -1 => forever')
parser.add_argument("--max-run-time-sec", default=-1, type=int,
                    help='max secs to run before early stopping. -1 => dont early stop')
parser.add_argument('--learning-rate', default=0.01, type=float, help='learning rate')
parser.add_argument('--momentum', default=0., type=float,
                    help='momentum (when applicable)')
parser.add_argument('--update-fn', default='vanilla',
                    help='vanilla (sgd) or rmsprop. not applied to embeddings')
parser.add_argument('--embedding-dim', default=100, type=int,
                    help='embedding node dimensionality')
parser.add_argument('--hidden-dim', default=50, type=int,
                    help='hidden node dimensionality')
parser.add_argument('--l2-penalty', default=0.0001, type=float,
                    help='l2 penalty for params')
parser.add_argument('--gru-initial-bias', default=2, type=int,
                    help='initial gru bias for r & z. higher => more like SimpleRnn')
opts = parser.parse_args()
print >>sys.stderr, opts

NUM_LABELS = 3

def log(s):
    print >>sys.stderr, util.dts(), s

# slurp training data, including converting of tokens -> ids
vocab = Vocab()
train_x, train_y, train_stats = util.load_data(opts.train_set, vocab,
                                               update_vocab=True,
                                               max_egs=int(opts.num_from_train))
log("train_stats %s %s" % (len(train_x), train_stats))
dev_x, dev_y, dev_stats = util.load_data(opts.dev_set, vocab,
                                         update_vocab=False,
                                         max_egs=int(opts.num_from_dev))
log("dev_stats %s %s" % (len(dev_x), dev_stats))

# input/output example vars
s1_idxs = T.ivector('s1')  # sequence for sentence one
s2_idxs = T.ivector('s2')  # sequence for sentence two
actual_y = T.ivector('y')  # single for sentence pair label; 0, 1 or 2

# keep track of different "layers" that handle their own gradients.
# includes rnns, final concat & softmax and, potentially, special handling for
# tied embeddings
layers = []

# build a bidirectional rnn of grus over s1
update_fn = globals().get(opts.update_fn)
if update_fn is None:
    raise Exception("unknown update function [%s]" % opts.update_fn)

h0 = theano.shared(np.zeros(opts.hidden_dim, dtype='float32'), name='h0', borrow=True)
s1_bidir = BidirectionalGruRnn('s1_bidir', vocab.size(), opts.embedding_dim, 
                               opts.hidden_dim, opts, update_fn, h0, s1_idxs)
layers.append(s1_bidir)

# build another pair of bidirectional rnn grus over s2
s2_bidir = BidirectionalGruRnn('s2_bidir', vocab.size(), opts.embedding_dim,
                               opts.hidden_dim, opts, update_fn, h0, s2_idxs)
layers.append(s2_bidir)

# build a unidirectional gru rnn over the bidirectional net over s2 and have it
# additionally conditioned on the context dervied from the networks over s1
s2_decoder = GruRnn(name='s2_decoder',
                    input_dim=2*opts.hidden_dim, hidden_dim=opts.hidden_dim,
                    opts=opts, update_fn=update_fn, h0=h0,
                    inputs=s2_bidir.all_states(),
                    context=s1_bidir.final_states(), context_dim=2*opts.hidden_dim)
layers.append(s2_decoder)

# use final state of this decoder to feed into the final MLP
concat_with_softmax = ConcatWithSoftmax(s2_decoder.final_state(), NUM_LABELS,
                                        opts.hidden_dim, update_fn)
layers.append(concat_with_softmax)
prob_y, pred_y = concat_with_softmax.prob_pred()

# calc l2_sum across all params
log(">l2 params")
params = [l.params_for_l2_penalty() for l in layers]
l2_sum = sum([(p**2).sum() for p in itertools.chain(*params)])

# calculate cost ; xent + l2 penalty
log("calc cost")
cross_entropy_cost = T.mean(T.nnet.categorical_crossentropy(prob_y, actual_y))
l2_cost = opts.l2_penalty * l2_sum
total_cost = cross_entropy_cost + l2_cost

# calculate updates
log("calc updates")
updates = []
for layer in layers:
    updates.extend(layer.updates_wrt_cost(total_cost, opts))

log("compiling")
train_fn = theano.function(inputs=[s1_idxs, s2_idxs, actual_y],
                           outputs=[total_cost],
                           updates=updates,
                           on_unused_input='ignore')  # on unused for debugging
test_fn = theano.function(inputs=[s1_idxs, s2_idxs, actual_y],
                          outputs=[pred_y, total_cost],
                          on_unused_input='ignore')

def stats_from_dev_set(stats):
    actuals = []
    predicteds  = []
    for (s1, s2), y in zip(dev_x, dev_y):
        pred_y, cost = test_fn(s1, s2, [y])
        actuals.append(y)
        predicteds.append(pred_y)
        stats.record_dev_cost(cost)
    dev_c = confusion_matrix(actuals, predicteds)
    dev_accuracy = util.accuracy(dev_c)
    stats.set_dev_accuracy(dev_accuracy)
    print "dev confusion\n %s (%s)" % (dev_c, dev_accuracy)


log("training")
epoch = 0
training_early_stop_time = opts.max_run_time_sec + time.time()
stats = Stats(os.path.basename(__file__), opts)
egs = zip(train_x, train_y)
while epoch != opts.num_epochs:
    random.shuffle(egs)
    for (s1, s2), y in egs:
        cost, = train_fn(s1, s2, [y])
        stats.record_training_cost(cost)
        early_stop = False
        if opts.max_run_time_sec != -1 and time.time() > training_early_stop_time:
            early_stop = True
        if stats.n_egs_trained % opts.dev_run_freq == 0 or early_stop:
            stats_from_dev_set(stats)
            stats.flush_to_stdout(epoch)
        if early_stop:
            exit(0)
    epoch += 1
