from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import tensorflow as tf
import numpy as np
import config
import sets
from functools import wraps as wraps
import tensorflow.contrib.slim as slim

from abc import ABCMeta, abstractmethod

def xavier_init(fan_in, fan_out, constant=1):
    """ Xavier initialization of network weights"""
    # https://stackoverflow.com/questions/33640581/how-to-do-xavier-initialization-on-tensorflow
    low = -constant*np.sqrt(6.0/(fan_in + fan_out))
    high = constant*np.sqrt(6.0/(fan_in + fan_out))
    return tf.random_uniform((fan_in, fan_out),
                             minval=low, maxval=high,
                             dtype=tf.float32)


def lazy_decorator(function):
    function_name = '_' + function.__name__

    @property
    @wraps(function)
    def lazy_call(self):
        if not hasattr(self, function_name):
            setattr(self, function_name, function(self))
        return getattr(self, function_name)
    return lazy_call


"""
network_architecture ={
"inputShape": [2,500,1],
"outputWidth": 500,
"numberOfFilters":[80,80],
"filterSize":[5,5],
"pool_size":3,
"pool_stride":3,
"FCwidth":1024,
"dropout":0.5
}
"""


class NNmodule(object):
    """
    Abstract Base Class for a generic Neural Network module that uses tensorflow

    """
    __metaclass__ = ABCMeta

    @abstractmethod
    def _create_network(self):
        pass

class NNscaffold(object):
    """
    Scaffold class to combine different NN modules at their final layers
    """
    def __init__(self, network_architecture,
                 learning_rate=0.001, batch_size=100):
        """
        Initiates a scaffold network with default values
        Inputs:
            network_architecture: A nested dictionary where the highest level
            keywords are the input names. e.g. {
                                                'NETseq':{
                                                            'inputShape':[2,500,1],
                                                            'outputWidth:500',
                                                            'numberOfFilters':[80,80]},
                                                'DNAseq':{
                                                            'inputShape':[4,500,1],
                                                            'outputWidth:500',
                                                            'numberOfFilters':[50,20]}}

        """
        self.network_architecture = network_architecture
        self.learning_rate = learning_rate
        self.batch_size = batch_size

        self.inputs={}
        # tf Graph input
        for key in network_architecture.keys():
            self.inputs[key] = tf.placeholder(tf.float32, [None] + network_architecture[key]["inputShape"],name=key)

        self.output = tf.placeholder(tf.float32, [None]+ network_architecture[key]["outputWidth"],name='output')
        self.dropout = tf.placeholder(tf.float32)

        self.net =list()

        self._encapsulate_models()


        # Define loss function based variational upper-bound and
        # corresponding optimizer
        self._create_loss_optimizer()


    def initialize(self,restore_dirs=None):
        """
        Initialize the scaffold model either from saved checkpoints (pre-trained)
        or from scratch
        """

        # Launch the session
        self.sess = tf.Session()
        if restore_dirs is not None:
            for key in network_architecture.keys():
                saver = tf.train.Saver([v.name for v in tf.trainable_variables() if key in v.name])
                saver.restore(self.sess,restore_dirs[key]+'model.ckpt')
                print('Session restored for '+key)
        else:
            # Initializing the tensor flow variables
            init = tf.initialize_all_variables()
            self.sess.run(init)
            print('Session initialized.')

    def _encapsulate_models(self):
        # Create Convolutional network
        for key in self.network_architecture.keys():
            with tf.variable_scope(key):
                self.net.append(self._create_network(key))


        combined_layer = tf.concat(1,self.net)


        with slim.arg_scope([slim.conv2d],
                     activation_fn=tf.nn.relu,
                     weights_initializer=tf.truncated_normal_initializer(0.0, 0.01),
                     weights_regularizer=slim.l2_regularizer(0.0005),padding='VALID',
                     stride=1):
            if len(self.net)>1:
                combined_layer = tf.reshape(combined_layer, shape=[-1,len(self.net), self.network_architecture[key]["FCwidth"], 1])
                self.net = slim.conv2d(combined_layer,
                                   40,
                                   [len(self.net),10],
                                   scope='conv1')
                self.net = slim.avg_pool2d(self.net, self.network_architecture[key]["pool_size"],
                                        stride=self.network_architecture[key]["pool_stride"],
                                        scope='pool2')
                self.net = slim.batch_norm(self.net,activation_fn=None)
                self.net = slim.flatten(self.net)
            else:
                self.net = combined_layer

            self.net = slim.dropout(self.net, self.dropout, scope='dropout3')
            self.net = slim.fully_connected(self.net,  self.network_architecture[key]["outputWidth"][0], activation_fn=None, scope='out')
            self.net = tf.nn.softmax(self.net)

    def _create_network(self,key):

         with slim.arg_scope([slim.conv2d],
                      activation_fn=tf.nn.relu,
                      weights_initializer=tf.truncated_normal_initializer(0.0, 0.01),
                      weights_regularizer=slim.l2_regularizer(0.0005),padding='VALID',
                      stride=1):

            net = slim.conv2d(  self.inputs[key],
                                self.network_architecture[key]['numberOfFilters'][0],
                                self.network_architecture[key]['filterSize'][0],
                                scope='conv1')
            net = slim.avg_pool2d(net,
                                    self.network_architecture[key]["pool_size"],
                                    stride=self.network_architecture[key]["pool_stride"],
                                    scope='pool1')
            net = slim.batch_norm(net,activation_fn=None)
            net = slim.conv2d(  net,
                                self.network_architecture[key]['numberOfFilters'][1],
                                self.network_architecture[key]['filterSize'][1],
                                scope='conv2')
            net = slim.avg_pool2d(net, self.network_architecture[key]["pool_size"],
                                    stride=self.network_architecture[key]["pool_stride"],
                                    scope='pool2')
            net = slim.batch_norm(net,activation_fn=None)
            net = slim.flatten(net)
            net = slim.dropout(net, self.dropout, scope='dropout2')
            net = slim.fully_connected(net,  self.network_architecture[key]["FCwidth"], scope='fc3')

            return net

    def _create_loss_optimizer(self):

        loss = tf.reduce_sum(tf.mul(self.output+1e-10,tf.sub(tf.log(self.output+1e-10),tf.log(self.net+1e-10))),1)

        self.cost = tf.reduce_mean(loss)   # average over batch
        width =  self.network_architecture.values()[0]["outputWidth"][0]

        target = tf.floor((10.*tf.cast(tf.argmax(self.output,dimension=1),tf.float32))/np.float(width))
        pred = tf.floor((10.*tf.cast(tf.argmax(self.net,dimension=1),tf.float32))/np.float(width))

        self.accuracy = tf.reduce_sum(tf.cast(tf.equal(pred,target),tf.int32))

        self.global_step = tf.Variable(0, name='globalStep', trainable=False)

        # Use ADAM optimizer
        self.optimizer = \
            tf.train.AdamOptimizer(learning_rate=self.learning_rate).minimize(self.cost,global_step = self.global_step)

    def train(self, trainInp,trainOut,accuracy=None):
        """Train model based on mini-batch of input data.

        Return cost of mini-batch.
        """
        train_feed = {self.output:trainOut.values()[0], self.dropout:self.network_architecture.values()[0]["dropout"]}
        train_feed.update({self.inputs[key]: trainInp[key] for key in self.network_architecture.keys()})

        if accuracy is not None:
            _ , cost,accuracy = self.sess.run((self.optimizer, self.cost, self.accuracy), feed_dict=train_feed)
        else:
            _ , cost = self.sess.run((self.optimizer, self.cost), feed_dict=train_feed)
            accuracy = None
        return cost,accuracy

    def test(self,testInp,testOut,accuracy=None):
        """Test model based on mini-batch of input data.

        Return cost of test.
        """
        if not hasattr(self,'test_feed'):
            self.test_feed = {self.output:testOut.values()[0], self.dropout:1}
            self.test_feed.update({self.inputs[key]: testInp[key] for key in self.network_architecture.keys()})
        if accuracy is not None:
            cost,accuracy = self.sess.run((self.cost, self.accuracy), feed_dict=self.test_feed)
        else:
            cost = self.sess.run( self.cost, feed_dict=self.test_feed)
            accuracy = None

        return cost,accuracy

    def getWeight(self,layerName):
        return self.sess.run([v for v in tf.trainable_variables() if v.name == layerName+'\weights:0'][0])


    def predict(self,testInp,testOut):
        """Return the result of a flow based on mini-batch of input data.

        """
        if not hasattr(self,'test_feed'):
            self.test_feed = {self.output:testOut.values()[0], self.dropout:1}
            self.test_feed.update({self.inputs[key]: testInp[key] for key in self.network_architecture.keys()})

        return self.sess.run( self.net, feed_dict=self.test_feed)

    def summarize(self,step):
        summaryStr = self.sess.run(self.summary_op, feed_dict=self.test_feed)
        self.summaryWriter.add_summary(summaryStr, step)
        self.summaryWriter.flush()

    def create_monitor_variables(self,savePath):
        # for monitoring
        tf.scalar_summary('KL divergence', self.cost)
        tf.scalar_summary('Accuracy', self.accuracy)
        self.summary_op = tf.merge_all_summaries()
        self.summaryWriter = tf.train.SummaryWriter(savePath, self.sess.graph)

######## DEPRECIATED ####################
# class ConvolutionalNeuralNetwork(object):
#     """
#     Convolutional Neural Network implementation
#     """
#     def __init__(self, network_architecture, input_name="DNAseq",
#                  learning_rate=0.001, batch_size=100):
#         self.network_architecture = network_architecture
#         self.learning_rate = learning_rate
#         self.batch_size = batch_size
#
#         # tf Graph input
#         self.x = tf.placeholder(tf.float32, [None] + network_architecture["inputShape"],name=input_name)
#         self.y = tf.placeholder(tf.float32, [None]+ network_architecture["outputWidth"],name='output')
#         self.dropout = tf.placeholder(tf.float32)
#
#         # Create Convolutional network
#         self._create_network()
#
#         # Define loss function based variational upper-bound and
#         # corresponding optimizer
#         self._create_loss_optimizer()
#
#         # Initializing the tensor flow variables
#         init = tf.initialize_all_variables()
#
#         # Launch the session
#         self.sess = tf.InteractiveSession()
#         self.sess.run(init)
#
#     def _create_network(self):
#
#          with slim.arg_scope([slim.conv2d],
#                       activation_fn=tf.nn.relu,
#                       weights_initializer=tf.truncated_normal_initializer(0.0, 0.01),
#                       weights_regularizer=slim.l2_regularizer(0.0005),padding='VALID',
#                       stride=1):
#
#             net = slim.conv2d(  self.x,
#                                 self.network_architecture['numberOfFilters'][0],
#                                 self.network_architecture['filterSize'][0],
#                                 scope='conv1')
#             net = slim.avg_pool2d(net,
#                                     self.network_architecture["pool_size"],
#                                     stride=self.network_architecture["pool_stride"],
#                                     scope='pool1')
#             net = slim.batch_norm(net,activation_fn=None)
#             net = slim.conv2d(  net,
#                                 self.network_architecture['numberOfFilters'][1],
#                                 self.network_architecture['filterSize'][1],
#                                 scope='conv2')
#             net = slim.avg_pool2d(net, self.network_architecture["pool_size"],
#                                     stride=self.network_architecture["pool_stride"],
#                                     scope='pool2')
#             net = slim.batch_norm(net,activation_fn=None)
#             net = slim.flatten(net)
#             net = slim.dropout(net, self.dropout, scope='dropout2')
#             net = slim.fully_connected(net,  self.network_architecture["FCwidth"], scope='fc3')
#             net = slim.dropout(net, self.dropout, scope='dropout3')
#             net = slim.fully_connected(net,  self.network_architecture["outputWidth"][0], activation_fn=None, scope='out')
#             self.net = tf.nn.softmax(net)
#
#
#     def _create_loss_optimizer(self):
#
#         loss = tf.reduce_sum(tf.mul(self.y+1e-10,tf.sub(tf.log(self.y+1e-10),tf.log(self.net+1e-10))),1)
#
#         self.cost = tf.reduce_mean(loss)   # average over batch
#
#         # Use ADAM optimizer
#         self.optimizer = \
#             tf.train.AdamOptimizer(learning_rate=self.learning_rate).minimize(self.cost)
#
#     def train(self, trainInp,trainOut):
#         """Train model based on mini-batch of input data.
#
#         Return cost of mini-batch.
#         """
#         _ , cost = self.sess.run((self.optimizer, self.cost),
#                                   feed_dict={self.x: trainInp,self.y: trainOut,self.dropout:self.network_architecture["dropout"]})
#         return cost
#
#     def test(self,testInp,testOut):
#         """Test model based on mini-batch of input data.
#
#         Return cost of test.
#         """
#         cost = self.sess.run( self.cost,
#                                   feed_dict={self.x: testInp ,self.y: testOut,self.dropout:1.})
#         return cost
#
#     def getWeight(self,layerName):
#         return self.sess.run([v for v in tf.trainable_variables() if v.name == layerName+'\weights:0'][0])
#
#
#     def predict(self,testInp,testOut):
#         """Return the result of a flow based on mini-batch of input data.
#
#         """
#         return self.sess.run( self.net,
#                                   feed_dict={self.x: testInp ,self.y: testOut,self.dropout:1.})