import numpy as np
import tensorflow as tf
from gym.spaces import Box, Discrete
import gym
import matplotlib.pyplot as plt
from playground.configs.manager import*
from gym.utils import colorize

from tensorflow.python.tools import inspect_checkpoint as chkp

from playground.policies.base import (
    BaseTFModelMixin,
    Policy,
    ReplayMemory,
    ReplayTrajMemory,
    Transition,
)
from playground.utils.tf_ops import dense_nn, conv2d_net, lstm_net


class DqnPolicy(Policy, BaseTFModelMixin):
    def __init__(self, env, name,
                 training=True,
                 gamma=0.99,
                 lr=0.001,
                 lr_decay=1.0,
                 epsilon=1.0,
                 epsilon_final=0.01,
                 batch_size=64,
                 memory_capacity=100000,
                 model_type="conv",
                 model_params=None,
                 step_size=1,  # only > 1 if model_type is 'lstm'.
                 layer_sizes=None,  # [64] by default.
                 target_update_type='hard',
                 target_update_params=None,
                 double_q=True,
                 dueling=True):
        """
        model_params: 'layer_sizes', 'step_size', 'lstm_layers', 'lstm_size'
        """
        Policy.__init__(self, env, name, gamma=gamma, training=training)
        BaseTFModelMixin.__init__(self, name, saver_max_to_keep=5)



        assert isinstance(self.env.action_space, Discrete)
        assert isinstance(self.env.observation_space, Box)
        assert model_type in ('dense', 'conv', 'lstm')
        assert step_size == 1 or model_type == 'lstm'
        assert target_update_type in ('hard', 'soft')

        self.gamma = gamma
        self.lr = lr
        self.lr_decay = lr_decay
        self.epsilon = epsilon
        self.epsilon_final = epsilon_final
        self.training = training

        self.model_type = model_type
        self.model_params = model_params or {}
        self.layer_sizes = layer_sizes or [32, 32]
        self.step_size = step_size
        self.double_q = double_q
        self.dueling = dueling

        self.target_update_type = target_update_type
        self.target_update_every_step = (target_update_params or {}).get('every_step', 100)
        self.target_update_tau = (target_update_params or {}).get('tau', 0.05)

        if self.model_type == 'lstm':
            self.memory = ReplayTrajMemory(capacity=memory_capacity, step_size=step_size)
        else:
            self.memory = ReplayMemory(capacity=memory_capacity)

        self.batch_size = batch_size

    @property
    def act_size(self):
        # Returns: An int
        return self.env.action_space.n

    @property
    def obs_size(self):
        # Returns: A list
        if self.model_type == 'dense':
            return [np.prod(list(self.env.observation_space.shape))]
        elif self.model_type in ('conv', 'lstm'):
            return list(self.env.observation_space.shape)
        else:
            assert NotImplementedError()

    def obs_to_inputs(self, ob):
        if self.model_type == 'dense':
            return ob.flatten()
        elif self.model_type == 'conv':
            #print(ob)
            return ob
        elif self.model_type == 'lstm':
            return ob
        else:
            assert NotImplementedError()

    def _init_target_q_net(self):
        self.sess.run([v_t.assign(v) for v_t, v in zip(self.q_target_vars, self.q_vars)])

    def _update_target_q_net_hard(self):
        self.sess.run([v_t.assign(v) for v_t, v in zip(self.q_target_vars, self.q_vars)])

    def _update_target_q_net_soft(self, tau=0.05):
        self.sess.run([v_t.assign(v_t * (1. - tau) + v * tau)
                       for v_t, v in zip(self.q_target_vars, self.q_vars)])

    def _extract_network_params(self):
        net_params = {}
        if self.model_type == 'dense':
            net_class = dense_nn
        elif self.model_type == 'conv':
            net_class = conv2d_net
            net_params={"conv_layers":self.model_params.get("conv_layers",2)}
        elif self.model_type == 'lstm':
            net_class = lstm_net
            net_params = {
                'lstm_layers': self.model_params.get('lstm_layers', 1),
                'lstm_size': self.model_params.get('lstm_size', 256),
                'step_size': self.step_size,
            }
        else:
            raise NotImplementedError("Unknown model type: '%s'" % self.model_type)

        return net_class, net_params

    def create_q_networks(self):
        # The first dimension should have batch_size * step_size
        self.states = tf.placeholder(tf.float32, shape=(None, *self.obs_size), name='state')
        self.states_next = tf.placeholder(tf.float32, shape=(None, *self.obs_size),
                                          name='state_next')
        self.actions = tf.placeholder(tf.int32, shape=(None,), name='action')
        self.actions_next = tf.placeholder(tf.int32, shape=(None,), name='action_next')
        self.rewards = tf.placeholder(tf.float32, shape=(None,), name='reward')
        self.done_flags = tf.placeholder(tf.float32, shape=(None,), name='done')

        # The output is a probability distribution over all the actions.
        net_class, net_params = self._extract_network_params()
        print(net_class)
        print(net_params)

        if self.dueling:
            self.q_hidden = net_class(self.states, self.layer_sizes[:-1], name='Q_primary',
                                      **net_params)
            self.adv = dense_nn(self.q_hidden, self.layer_sizes[-1:] + [self.act_size],
                                name='Q_primary_adv')
            self.v = dense_nn(self.q_hidden, self.layer_sizes[-1:] + [1], name='Q_primary_v')
            # Average Dueling
            self.q = self.v + (self.adv - tf.reduce_mean(self.adv, reduction_indices=1, keep_dims=True))
            self.q_target_hidden = net_class(self.states_next, self.layer_sizes[:-1], name='Q_target',
                                             **net_params)
            self.adv_target = dense_nn(self.q_target_hidden, self.layer_sizes[-1:] + [self.act_size],
                                       name='Q_target_adv')
            self.v_target = dense_nn(self.q_target_hidden, self.layer_sizes[-1:] + [1],
                                     name='Q_target_v')

            # Average Dueling
            self.q_target = self.v_target + (self.adv_target - tf.reduce_mean(
                self.adv_target, reduction_indices=1, keep_dims=True))

        else:
            self.q = net_class(self.states, self.layer_sizes + [self.act_size], name='Q_primary',
                               **net_params)
            self.q_target = net_class(self.states_next, self.layer_sizes + [self.act_size],
                                      name='Q_target', **net_params)

        # The primary and target Q networks should match.
        self.load_model()
        self.q_vars = self.scope_vars('Q_primary')
        self.q_target_vars = self.scope_vars('Q_target')
#        print(self.sess.run(self.q_vars))
        assert len(self.q_vars) == len(self.q_target_vars), "Two Q-networks are not same."

    def build(self):
            self.create_q_networks()
            self.actions_selected_by_q = tf.argmax(self.q, axis=-1, name='action_selected')
            action_one_hot = tf.one_hot(self.actions, self.act_size, 1.0, 0.0, name='action_one_hot')
            pred = tf.reduce_sum(self.q * action_one_hot, reduction_indices=-1, name='q_acted')

            if self.double_q:
                actions_next_flatten = self.actions_next + tf.range(
                    0, self.batch_size * self.step_size) * self.q_target.shape[1]
                max_q_next_target = tf.gather(tf.reshape(self.q_target, [-1]), actions_next_flatten)
            else:
                max_q_next_target = tf.reduce_max(self.q_target, axis=-1)

            y = self.rewards + (1. - self.done_flags) * self.gamma * max_q_next_target

            self.learning_rate = tf.placeholder(tf.float32, shape=None, name='learning_rate')
            self.loss = tf.reduce_mean(tf.square(pred - tf.stop_gradient(y)), name="loss_mse_train")
            self.optimizer = tf.train.AdamOptimizer(self.learning_rate).minimize(self.loss, name="adam_optim")

            with tf.variable_scope('summary'):
                q_summ = []
                avg_q = tf.reduce_mean(self.q, 0)
                for idx in range(self.act_size):
                    q_summ.append(tf.summary.histogram('q/%s' % idx, avg_q[idx]))
                self.q_summ = tf.summary.merge(q_summ, 'q_summary')

                self.q_y_summ = tf.summary.histogram("batch/y", y)
                self.q_pred_summ = tf.summary.histogram("batch/pred", pred)
                self.loss_summ = tf.summary.scalar("loss", self.loss)

                self.ep_reward = tf.placeholder(tf.float32, name='episode_reward')
                self.ep_reward_summ = tf.summary.scalar('episode_reward', self.ep_reward)

                self.merged_summary = tf.summary.merge_all(key=tf.GraphKeys.SUMMARIES)

            self.sess.run(tf.global_variables_initializer())
            self._init_target_q_net()

    def update_target_q_net(self, step):
        if self.target_update_type == 'hard':
            if step % self.target_update_every_step == 0:
                self._update_target_q_net_hard()
        else:
            self._update_target_q_net_soft(self.target_update_tau)

    def act(self, state, epsilon=0.1):
        self.env.render()
        #print("moving")
        if self.training and np.random.random() < epsilon:
            return env.action_space.sample()

        with self.sess.as_default():
            if self.model_type == 'lstm':
                return self.actions_selected_by_q.eval({
                    self.states: [np.zeros(state.shape)] * (self.step_size - 1) + [state]
                })[-1]
            else:
                return self.actions_selected_by_q.eval({self.states: [state]})[-1]

    def train(self, n_episodes=5, annealing_episodes=None, every_episode=None):
        step = 0
        i = 0
        reward = 0.
        reward_history = [0.0]
        reward_averaged = []

        lr = self.lr
        eps = self.epsilon
        annealing_episodes = annealing_episodes or n_episodes
        eps_drop = (self.epsilon - self.epsilon_final) / annealing_episodes
        print("eps_drop:", eps_drop)


#if there is already a trained model, then start to play instead of training
        if self.load_model():
            done=False
            traj = []
            ob = self.env.reset()
            while not done:
                a = self.act(self.obs_to_inputs(ob), eps)
                new_ob, r, done, info = self.env.step(a)
                # print(step)
                # print(done)
                step += 1
                reward += r

                traj.append(
                    Transition(self.obs_to_inputs(ob), a, r, self.obs_to_inputs(new_ob), done))
                ob = new_ob

                # No enough samples in the buffer yet.
                if self.memory.size < self.batch_size:
                    continue

                # Training with a mini batch of samples!
                batch_data = self.memory.sample(self.batch_size)
                feed_dict = {
                    self.learning_rate: lr,
                    self.states: batch_data['s'],
                    self.actions: batch_data['a'],
                    self.rewards: batch_data['r'],
                    self.states_next: batch_data['s_next'],
                    self.done_flags: batch_data['done'],
                    self.ep_reward: reward_history[-1],
                }

                if self.double_q:
                    actions_next = self.sess.run(self.actions_selected_by_q, {
                        self.states: batch_data['s_next']
                    })
                    feed_dict.update({self.actions_next: actions_next})

                _, q_val, q_target_val, loss, summ_str = self.sess.run(
                    [self.optimizer, self.q, self.q_target, self.loss, self.merged_summary],
                    feed_dict
                )
                self.writer.add_summary(summ_str, step)
                self.update_target_q_net(step)
            print("final reward:",reward)
            return None


        else:
            for n_episode in range(n_episodes):
                i = i + 1
                ob = self.env.reset()
                print(ob.shape)
                done = False
                traj = []

                while not done:
                    a = self.act(self.obs_to_inputs(ob), eps)
                    new_ob, r, done, info = self.env.step(a)
                    # print(step)
                    # print(done)
                    step += 1
                    reward += r

                    traj.append(
                        Transition(self.obs_to_inputs(ob), a, r, self.obs_to_inputs(new_ob), done))
                    ob = new_ob

                    # No enough samples in the buffer yet.
                    if self.memory.size < self.batch_size:
                        continue

                    # Training with a mini batch of samples!
                    batch_data = self.memory.sample(self.batch_size)
                    feed_dict = {
                        self.learning_rate: lr,
                        self.states: batch_data['s'],
                        self.actions: batch_data['a'],
                        self.rewards: batch_data['r'],
                        self.states_next: batch_data['s_next'],
                        self.done_flags: batch_data['done'],
                        self.ep_reward: reward_history[-1],
                    }

                    if self.double_q:
                        actions_next = self.sess.run(self.actions_selected_by_q, {
                            self.states: batch_data['s_next']
                        })
                        feed_dict.update({self.actions_next: actions_next})

                    _, q_val, q_target_val, loss, summ_str = self.sess.run(
                        [self.optimizer, self.q, self.q_target, self.loss, self.merged_summary],
                        feed_dict
                    )
                    self.writer.add_summary(summ_str, step)
                    self.update_target_q_net(step)

                # Add all the transitions of one trajectory into the replay memory.
                self.memory.add(traj)

                # One episode is complete.
                reward_history.append(reward)
                reward_averaged.append(np.mean(reward_history[-10:]))
                reward = 0.

                # Annealing the learning and exploration rate after every episode.
                lr *= self.lr_decay
                if eps > self.epsilon_final:
                    eps -= eps_drop

                if not (reward_history and every_episode and n_episode % every_episode) == 0:
                    # Report the performance every `every_step` steps
                    print(
                        "[episodes:{}/step:{}], best:{}, avg:{:.2f}:{}, lr:{:.4f}, eps:{:.4f}".format(
                            n_episode, step, np.max(reward_history),
                            np.mean(reward_history[-10:]), reward_history[-5:],
                            lr, eps, self.memory.size
                        ))
                #  print(i,done)
                print("episode", i, " done")

        self.save_model(step=step)

        print("[FINAL] episodes: {}, Max reward: {}, Average reward: {}".format(
                len(reward_history), np.max(reward_history), np.mean(reward_history)))

        data_dict = {
                'reward': reward_history,
                'reward_smooth10': reward_averaged,
            }
        print(data_dict)
        plot_learning_curve(self.model_name, data_dict, xlabel='episode')


    def checkpoint_dir(self):
            ckpt_path = os.path.join(r"C:\Users\qianp\PycharmProjects\assi2", 'checkpoints', self.model_name)
            os.makedirs(ckpt_path, exist_ok=True)
            return ckpt_path

    def load_model(self):
        print(colorize(" [*] Loading checkpoints...", "green"))
        ckpt_path = os.path.join(r"C:\Users\qianp\PycharmProjects\assi2", 'checkpoints', self.model_name)

        ckpt = tf.train.get_checkpoint_state(ckpt_path)
        print(self.checkpoint_dir, ckpt)
        if ckpt and ckpt.model_checkpoint_path:
            ckpt_name = os.path.basename(ckpt.model_checkpoint_path)
            print(ckpt_name)
            fname = os.path.join(ckpt_path, ckpt_name)
            print(fname)
            self.saver.restore(self.sess, fname)
            chkp.print_tensors_in_checkpoint_file(fname,None,True)  # bool 是否打印所有的tensor的name
            print(colorize(" [*] Load SUCCESS: %s" % fname, "green"))
            return True
        else:
            print(colorize(" [!] Load FAILED: %s" % self.checkpoint_dir, "red"))
            return False

def plot_learning_curve(filename, value_dict, xlabel='step'):
    # Plot step vs the mean(last 50 episodes' rewards)
    fig = plt.figure(figsize=(12, 4 * len(value_dict)))

    for i, (key, values) in enumerate(value_dict.items()):
        ax = fig.add_subplot(len(value_dict), 1, i + 1)
        ax.plot(range(len(values)), values)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(key)
        ax.grid('k--', alpha=0.6)

    plt.show()
    #plt.tight_layout()





env=gym.make('MsPacman-v0')
name='lstm'
obj=DqnPolicy(env,name,model_type="lstm")
obj.build()
#if obj.load_model():
 #   pass
#else:
obj.train(n_episodes=30)
print("Training completed:", name)