#!/usr/bin/env python3
#
#    This file is part of Leela Zero.
#    Copyright (C) 2017 Gian-Carlo Pascutto
#
#    Leela Zero is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Leela Zero is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Leela Zero.  If not, see <http://www.gnu.org/licenses/>.

import os
import numpy as np
import time
import tensorflow as tf

def weight_variable(shape):
    initial = tf.truncated_normal(shape, stddev=0.1)
    return tf.Variable(initial)

# Bias weights for layers not followed by BatchNorm
def bias_variable(shape):
    initial = tf.constant(0.0, shape=shape)
    return tf.Variable(initial)

# No point in learning bias weights as they are cancelled
# out by the BatchNorm layers's mean adjustment.
def bn_bias_variable(shape):
    initial = tf.constant(0.0, shape=shape)
    return tf.Variable(initial, trainable=False)

def conv2d(x, W):
    return tf.nn.conv2d(x, W, data_format='NCHW',
                        strides=[1, 1, 1, 1], padding='SAME')

class TFProcess:
    def __init__(self, next_batch):
        gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=0.75)
        config = tf.ConfigProto(gpu_options=gpu_options)
        self.session = tf.Session(config=config)

        # For exporting
        self.weights = []

        # TF variables
        self.next_batch = next_batch
        self.global_step = tf.Variable(0, name='global_step', trainable=False)
        self.x = next_batch[0]  # tf.placeholder(tf.float32, [None, 18, 19 * 19])
        self.y_ = next_batch[1] # tf.placeholder(tf.float32, [None, 362])
        self.z_ = next_batch[2] # tf.placeholder(tf.float32, [None, 1])
        self.training = tf.placeholder(tf.bool)
        self.batch_norm_count = 0
        self.y_conv, self.z_conv = self.construct_net(self.x)

        # Calculate loss on policy head
        cross_entropy = \
            tf.nn.softmax_cross_entropy_with_logits(labels=self.y_,
                                                    logits=self.y_conv)
        self.policy_loss = tf.reduce_mean(cross_entropy)
        tf.summary.scalar('policy_loss', self.policy_loss)

        # Loss on value head
        self.mse_loss = \
            tf.reduce_mean(tf.squared_difference(self.z_, self.z_conv))
        tf.summary.scalar('mse_loss', self.mse_loss)

        # Regularizer
        regularizer = tf.contrib.layers.l2_regularizer(scale=0.0001)
        reg_variables = tf.trainable_variables()
        reg_term = \
            tf.contrib.layers.apply_regularization(regularizer, reg_variables)

        loss = 1.0 * self.policy_loss + 1.0 * self.mse_loss + reg_term

        opt_op = tf.train.MomentumOptimizer(
            learning_rate=0.05, momentum=0.9, use_nesterov=True)

        self.update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(self.update_ops):
            self.train_op = \
                opt_op.minimize(loss, global_step=self.global_step)

        correct_prediction = \
            tf.equal(tf.argmax(self.y_conv, 1), tf.argmax(self.y_, 1))
        correct_prediction = tf.cast(correct_prediction, tf.float32)
        self.accuracy = tf.reduce_mean(correct_prediction)
        tf.summary.scalar('accuracy', self.accuracy)

        self.avg_policy_loss = None
        self.avg_mse_loss = None
        self.time_start = None

        self.init = tf.global_variables_initializer()
        self.saver = tf.train.Saver()

        self.session.run(self.init)

    def restore(self, file):
        print("Restoring from {0}".format(file))
        self.saver.restore(self.session, file)

    def process(self, batch_size):
        # Run training for this batch
        policy_loss, mse_loss, _, _ = self.session.run(
            [self.policy_loss, self.mse_loss, self.train_op, self.next_batch],
            feed_dict={self.training: True})
        steps = tf.train.global_step(self.session, self.global_step)
        # Keep running averages
        # XXX: use built-in support like tf.moving_average_variables?
        # Google's paper scales MSE by 1/4 to a [0, 1] range, so do the same to
        # get comparable values.
        mse_loss = mse_loss / 4.0
        if self.avg_policy_loss:
            self.avg_policy_loss = 0.99 * self.avg_policy_loss + 0.01 * policy_loss
        else:
            self.avg_policy_loss = policy_loss
        if self.avg_mse_loss:
            self.avg_mse_loss = 0.99 * self.avg_mse_loss + 0.01 * mse_loss
        else:
            self.avg_mse_loss = mse_loss
        if steps % 100 == 0:
            time_end = time.time()
            speed = 0
            if self.time_start:
                elapsed = time_end - self.time_start
                speed = batch_size * (100.0 / elapsed)
            print("step {}, policy loss={:g} mse={:g} ({:g} pos/s)".format(
                steps, self.avg_policy_loss, self.avg_mse_loss, speed))
            self.time_start = time_end
        # Ideally this would use a seperate dataset and so on...
        if steps % 2000 == 0:
            sum_accuracy = 0
            sum_mse = 0
            for _ in range(0, 10):
                train_accuracy, _ = self.session.run(
                    [self.accuracy, self.next_batch],
                    feed_dict={self.training: False})
                train_mse, _ = self.session.run(
                    [self.mse_loss, self.next_batch],
                    feed_dict={self.training: False})
                sum_accuracy += train_accuracy
                sum_mse += train_mse
            sum_accuracy /= 10.0
            # Additionally rescale to [0, 1] so divide by 4
            sum_mse /= (4.0 * 10.0)
            print("step {}, training accuracy={:g}%, mse={:g}".format(
                steps, sum_accuracy*100.0, sum_mse))
            path = os.path.join(os.getcwd(), "leelaz-model")
            save_path = self.saver.save(self.session, path, global_step=steps)
            print("Model saved in file: {}".format(save_path))
            leela_path = path + ".txt"
            self.save_leelaz_weights(leela_path)
            print("Leela weights saved to {}".format(leela_path))

    def save_leelaz_weights(self, filename):
        with open(filename, "w") as file:
            # Version tag
            file.write("1")
            for weights in self.weights:
                # Newline unless last line (single bias)
                file.write("\n")
                work_weights = None
                # Keyed batchnorm weights
                if isinstance(weights, str):
                    work_weights = tf.get_default_graph().get_tensor_by_name(weights)
                elif weights.shape.ndims == 4:
                    # Convolution weights need a transpose
                    #
                    # TF (kYXInputOutput)
                    # [filter_height, filter_width, in_channels, out_channels]
                    #
                    # Leela/cuDNN/Caffe (kOutputInputYX)
                    # [output, input, filter_size, filter_size]
                    work_weights = tf.transpose(weights, [3, 2, 0, 1])
                elif weights.shape.ndims == 2:
                    # Fully connected layers are [in, out] in TF
                    #
                    # [out, in] in Leela
                    #
                    work_weights = tf.transpose(weights, [1, 0])
                else:
                    # Biases, batchnorm etc
                    work_weights = weights
                nparray = work_weights.eval(session=self.session)
                wt_str = [str(wt) for wt in np.ravel(nparray)]
                file.write(" ".join(wt_str))

    def get_batchnorm_key(self):
        result = "bn" + str(self.batch_norm_count)
        self.batch_norm_count += 1
        return result

    def conv_block(self, inputs, filter_size, input_channels, output_channels):
        W_conv = weight_variable([filter_size, filter_size,
                                  input_channels, output_channels])
        b_conv = bn_bias_variable([output_channels])
        self.weights.append(W_conv)
        self.weights.append(b_conv)
        # The weights are internal to the batchnorm layer, so apply
        # a unique scope that we can store, and use to look them back up
        # later on.
        weight_key = self.get_batchnorm_key()
        self.weights.append(weight_key + "/batch_normalization/moving_mean:0")
        self.weights.append(weight_key + "/batch_normalization/moving_variance:0")

        with tf.variable_scope(weight_key):
            h_bn = \
                tf.layers.batch_normalization(
                    tf.nn.bias_add(conv2d(inputs, W_conv),
                                   b_conv, data_format='NCHW'),
                    epsilon=1e-5, axis=1, fused=True,
                    center=False, scale=False,
                    training=self.training)
        h_conv = tf.nn.relu(h_bn)
        return h_conv

    def residual_block(self, inputs, channels):
        # First convnet
        orig = tf.identity(inputs)
        W_conv_1 = weight_variable([3, 3, channels, channels])
        b_conv_1 = bn_bias_variable([channels])
        self.weights.append(W_conv_1)
        self.weights.append(b_conv_1)
        weight_key_1 = self.get_batchnorm_key()
        self.weights.append(weight_key_1 + "/batch_normalization/moving_mean:0")
        self.weights.append(weight_key_1 + "/batch_normalization/moving_variance:0")

        # Second convnet
        W_conv_2 = weight_variable([3, 3, channels, channels])
        b_conv_2 = bn_bias_variable([channels])
        self.weights.append(W_conv_2)
        self.weights.append(b_conv_2)
        weight_key_2 = self.get_batchnorm_key()
        self.weights.append(weight_key_2 + "/batch_normalization/moving_mean:0")
        self.weights.append(weight_key_2 + "/batch_normalization/moving_variance:0")

        with tf.variable_scope(weight_key_1):
            h_bn1 = \
                tf.layers.batch_normalization(
                    tf.nn.bias_add(conv2d(inputs, W_conv_1),
                                   b_conv_1, data_format='NCHW'),
                    epsilon=1e-5, axis=1, fused=True,
                    center=False, scale=False,
                    training=self.training)
        h_out_1 = tf.nn.relu(h_bn1)
        with tf.variable_scope(weight_key_2):
            h_bn2 = \
                tf.layers.batch_normalization(
                    tf.nn.bias_add(conv2d(h_out_1, W_conv_2),
                                   b_conv_2, data_format='NCHW'),
                    epsilon=1e-5, axis=1, fused=True,
                    center=False, scale=False,
                    training=self.training)
        h_out_2 = tf.nn.relu(tf.add(h_bn2, orig))
        return h_out_2

    def construct_net(self, planes):
        # NCHW format
        # batch, 18 channels, 19 x 19
        x_planes = tf.reshape(planes, [-1, 18, 19, 19])

        conv1 = self.conv_block(x_planes, filter_size=3,
                                input_channels=18, output_channels=128)
        conv2 = self.residual_block(conv1, 128)
        conv3 = self.residual_block(conv2, 128)
        conv4 = self.residual_block(conv3, 128)
        conv5 = self.residual_block(conv4, 128)
        conv6 = self.residual_block(conv5, 128)
        conv7 = self.residual_block(conv6, 128)

        # Policy head
        conv8 = self.conv_block(conv7, filter_size=1,
                                input_channels=128, output_channels=2)
        h_conv8_flat = tf.reshape(conv8, [-1, 2*19*19])
        W_fc1 = weight_variable([2 * 19 * 19, (19 * 19) + 1])
        b_fc1 = bias_variable([(19 * 19) + 1])
        self.weights.append(W_fc1)
        self.weights.append(b_fc1)
        h_fc1 = tf.add(tf.matmul(h_conv8_flat, W_fc1), b_fc1)

        # Value head
        conv9 = self.conv_block(conv7, filter_size=1,
                                input_channels=128, output_channels=1)
        h_conv9_flat = tf.reshape(conv9, [-1, 19*19])
        W_fc2 = weight_variable([19 * 19, 256])
        b_fc2 = bias_variable([256])
        self.weights.append(W_fc2)
        self.weights.append(b_fc2)
        h_fc2 = tf.nn.relu(tf.add(tf.matmul(h_conv9_flat, W_fc2), b_fc2))
        W_fc3 = weight_variable([256, 1])
        b_fc3 = bias_variable([1])
        self.weights.append(W_fc3)
        self.weights.append(b_fc3)
        h_fc3 = tf.nn.tanh(tf.add(tf.matmul(h_fc2, W_fc3), b_fc3))

        return h_fc1, h_fc3
