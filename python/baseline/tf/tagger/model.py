from baseline.tf.tfy import *
import json
import os
from google.protobuf import text_format
from tensorflow.python.platform import gfile
from tensorflow.contrib.layers import fully_connected, xavier_initializer
from baseline.model import Tagger, create_tagger_model, load_tagger_model


class RNNTaggerModel(Tagger):

    def save_values(self, basename):
        self.saver.save(self.sess, basename)

    def save_md(self, basename):

        path = basename.split('/')
        base = path[-1]
        outdir = '/'.join(path[:-1])

        tf.train.write_graph(self.sess.graph_def, outdir, base + '.graph', as_text=False)
        with open(basename + '.saver', 'w') as f:
            f.write(str(self.saver.as_saver_def()))

        with open(basename + '.labels', 'w') as f:
            json.dump(self.labels, f)

        if len(self.word_vocab) > 0:
            with open(basename + '-word.vocab', 'w') as f:
                json.dump(self.word_vocab, f)

        with open(basename + '-char.vocab', 'w') as f:
            json.dump(self.char_vocab, f)

    def make_feed_dict(self, x, xch, lengths, y=None, do_dropout=False):
        pkeep = 1.0-self.pdrop_value if do_dropout else 1.0
        feed_dict = {self.x: x, self.xch: xch, self.lengths: lengths, self.pkeep: pkeep}
        if y is not None:
            feed_dict[self.y] = y
        return feed_dict

    def save(self, basename):
        self.save_md(basename)
        self.save_values(basename)

    @staticmethod
    def load(basename, **kwargs):
        model = RNNTaggerModel()
        model.sess = kwargs.get('sess', tf.Session())
        checkpoint_name = kwargs.get('checkpoint_name', basename)
        checkpoint_name = checkpoint_name or basename
        with open(basename + '.saver') as fsv:
            saver_def = tf.train.SaverDef()
            text_format.Merge(fsv.read(), saver_def)
            print('Loaded saver def')

        with gfile.FastGFile(basename + '.graph', 'rb') as f:
            gd = tf.GraphDef()
            gd.ParseFromString(f.read())
            model.sess.graph.as_default()
            tf.import_graph_def(gd, name='')
            print('Imported graph def')

            model.sess.run(saver_def.restore_op_name, {saver_def.filename_tensor_name: checkpoint_name})
            model.x = tf.get_default_graph().get_tensor_by_name('x:0')
            model.xch = tf.get_default_graph().get_tensor_by_name('xch:0')
            model.y = tf.get_default_graph().get_tensor_by_name('y:0')
            model.lengths = tf.get_default_graph().get_tensor_by_name('lengths:0')
            model.pkeep = tf.get_default_graph().get_tensor_by_name('pkeep:0')
            model.best = tf.get_default_graph().get_tensor_by_name('output/ArgMax:0')
            model.probs = tf.get_default_graph().get_tensor_by_name('output/Reshape_1:0')  # TODO: rename
            try:
                model.A = tf.get_default_graph().get_tensor_by_name('Loss/transitions:0')
                print('Found transition matrix in graph, setting crf=True')
                model.crf = True
            except:
                print('Failed to get transition matrix, setting crf=False')
                model.A = None
                model.crf = False

        with open(basename + '.labels', 'r') as f:
            model.labels = json.load(f)

        model.word_vocab = {}
        if os.path.exists(basename + '-word.vocab'):
            with open(basename + '-word.vocab', 'r') as f:
                model.word_vocab = json.load(f)

        with open(basename + '-char.vocab', 'r') as f:
            model.char_vocab = json.load(f)

        model.saver = tf.train.Saver(saver_def=saver_def)
        return model

    def save_using(self, saver):
        self.saver = saver

    def _compute_word_level_loss(self, mask):

        nc = len(self.labels)
        # Cross entropy loss
        cross_entropy = tf.one_hot(self.y, nc, axis=-1) * tf.log(tf.nn.softmax(self.probs))
        cross_entropy = -tf.reduce_sum(cross_entropy, reduction_indices=2)
        cross_entropy *= mask
        cross_entropy = tf.reduce_sum(cross_entropy, reduction_indices=1)
        all_loss = tf.reduce_mean(cross_entropy, name="loss")
        return all_loss

    def _compute_sentence_level_loss(self):

        ll, self.A = tf.contrib.crf.crf_log_likelihood(self.probs, self.y, self.lengths)
        return tf.reduce_mean(-ll)

    def create_loss(self):

        with tf.variable_scope("Loss"):
            gold = tf.cast(self.y, tf.float32)
            mask = tf.sign(gold)

            if self.crf is True:
                print('crf=True, creating SLL')
                all_loss = self._compute_sentence_level_loss()
            else:
                print('crf=False, creating WLL')
                all_loss = self._compute_word_level_loss(mask)

        return all_loss

    def __init__(self):
        super(RNNTaggerModel, self).__init__()
        pass

    def get_vocab(self, vocab_type='word'):
        return self.word_vocab if vocab_type == 'word' else self.char_vocab

    def get_labels(self):
        return self.labels

    def predict(self, x, xch, lengths):

        feed_dict = self.make_feed_dict(x, xch, lengths)
        # We can probably conditionally add the loss here
        preds = []
        if self.crf is True:
            probv, tranv = self.sess.run([self.probs, self.A], feed_dict=feed_dict)

            for pij, sl in zip(probv, lengths):
                unary = pij[:sl]
                viterbi, _ = tf.contrib.crf.viterbi_decode(unary, tranv)
                preds.append(viterbi)
        else:
            # Get batch (B, T)
            bestv = self.sess.run(self.best, feed_dict=feed_dict)
            # Each sentence, probv
            for pij, sl in zip(bestv, lengths):
                unary = pij[:sl]
                preds.append(unary)

        return preds

    @staticmethod
    def create(labels, word_vec, char_vec, **kwargs):

        model = RNNTaggerModel()
        model.sess = kwargs.get('sess', tf.Session())

        mxlen = kwargs.get('maxs', 100)
        maxw = kwargs.get('maxw', 100)
        wsz = kwargs.get('wsz', 30)
        filtsz = kwargs.get('cfiltsz')
        hsz = int(kwargs['hsz'])
        pdrop = kwargs.get('dropout', 0.5)
        rnntype = kwargs.get('rnntype', 'blstm')
        nlayers = kwargs.get('layers', 1)
        model.labels = labels
        model.crf = bool(kwargs.get('crf', False))
        char_dsz = char_vec.dsz
        nc = len(labels)
        model.x = kwargs.get('x', tf.placeholder(tf.int32, [None, mxlen], name="x"))
        model.xch = kwargs.get('xch', tf.placeholder(tf.int32, [None, mxlen, maxw], name="xch"))
        model.y = kwargs.get('y', tf.placeholder(tf.int32, [None, mxlen], name="y"))
        model.lengths = kwargs.get('lengths', tf.placeholder(tf.int32, [None], name="lengths"))
        model.pkeep = kwargs.get('pkeep', tf.placeholder(tf.float32, name="pkeep"))
        model.pdrop_value = pdrop
        model.word_vocab = {}
        if word_vec is not None:
            model.word_vocab = word_vec.vocab
        model.char_vocab = char_vec.vocab
        seed = np.random.randint(10e8)
        if word_vec is not None:
            with tf.name_scope("WordLUT"):
                Ww = tf.Variable(tf.constant(word_vec.weights, dtype=tf.float32), name="W")

                we0 = tf.scatter_update(Ww, tf.constant(0, dtype=tf.int32, shape=[1]), tf.zeros(shape=[1, word_vec.dsz]))

                with tf.control_dependencies([we0]):
                    wembed = tf.nn.embedding_lookup(Ww, model.x, name="embeddings")

        Wch = tf.Variable(tf.constant(char_vec.weights, dtype=tf.float32), name="Wch")
        ce0 = tf.scatter_update(Wch, tf.constant(0, dtype=tf.int32, shape=[1]), tf.zeros(shape=[1, char_dsz]))

        with tf.variable_scope("Chars2Word"):
            with tf.control_dependencies([ce0]):
                rnnchar_bt_x_w = tf.reshape(model.xch, [-1, maxw])
                mxfiltsz = np.max(filtsz)
                halffiltsz = mxfiltsz // 2
                zeropad = tf.pad(rnnchar_bt_x_w, [[0, 0], [halffiltsz, halffiltsz]], "CONSTANT")
                cembed = tf.nn.embedding_lookup(Wch, zeropad, name="embeddings")
                cmot = char_word_conv_embeddings(cembed, filtsz, char_dsz, wsz)
                word_char = tf.reshape(cmot, [-1, mxlen, len(filtsz) * wsz])

        # Join embeddings along the third dimension
        joint = word_char if word_vec is None else tf.concat(values=[wembed, word_char], axis=2)
        embedseq = tf.nn.dropout(joint, model.pkeep)

        if rnntype == 'blstm':
            rnnfwd = stacked_lstm(hsz, model.pkeep, nlayers)
            rnnbwd = stacked_lstm(hsz, model.pkeep, nlayers)
            rnnout, _ = tf.nn.bidirectional_dynamic_rnn(rnnfwd, rnnbwd, embedseq, sequence_length=model.lengths, dtype=tf.float32)
            # The output of the BRNN function needs to be joined on the H axis
            rnnout = tf.concat(axis=2, values=rnnout)
        else:
            rnnfwd = stacked_lstm(hsz, model.pkeep, nlayers)
            rnnout, _ = tf.nn.dynamic_rnn(rnnfwd, embedseq, sequence_length=model.lengths, dtype=tf.float32)
        with tf.variable_scope("output"):
            # Converts seq to tensor, back to (B,T,W)

            hout = hsz
            if rnntype == 'blstm':
                hout *= 2
            # Flatten from [B x T x H] - > [BT x H]
            rnnout_bt_x_h = tf.reshape(rnnout, [-1, hout])

            #init = tf.random_uniform_initializer(-0.05, 0.05, dtype=tf.float32, seed=seed)
            init = xavier_initializer(True, seed)

            with tf.contrib.slim.arg_scope([fully_connected], weights_initializer=init):
                hidden = tf.nn.dropout(fully_connected(rnnout_bt_x_h, hsz, activation_fn=tf.nn.tanh), model.pkeep)
                preds = fully_connected(hidden, nc, activation_fn=None, weights_initializer=init)
            model.probs = tf.reshape(preds, [-1, mxlen, nc])
            model.best = tf.argmax(model.probs, 2)
        return model


def create_model(labels, word_embedding, char_embedding, **kwargs):
    model = create_tagger_model(RNNTaggerModel.create, labels, word_embedding, char_embedding, **kwargs)
    return model


def load_model(modelname, **kwargs):
    return load_tagger_model(RNNTaggerModel.load, modelname, **kwargs)
