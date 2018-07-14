import os
import sys
import time
import argparse
import numpy as np
from glob import glob
from scipy.misc import imshow
from comet_ml import Experiment
import torch
import torchvision
from torch import nn
from torch import autograd
from torch import optim
from torch.nn import functional as F

import ops
import plot
import utils


def load_args():

    parser = argparse.ArgumentParser(description='param-wgan')
    parser.add_argument('-z', '--dim', default=128, type=int, help='latent space size')
    parser.add_argument('-g', '--gp', default=10, type=int, help='gradient penalty')
    parser.add_argument('-b', '--batch_size', default=256, type=int)
    parser.add_argument('-e', '--epochs', default=200000, type=int)
    parser.add_argument('-o', '--output_dim', default=784, type=int)
    parser.add_argument('-m', '--model', default='conv', type=str)
    parser.add_argument('-s', '--size', default='wide7', type=str)
    parser.add_argument('-d', '--dataset', default='mnist', type=str)
    parser.add_argument('-l', '--layer', default='conv2', type=str)
    parser.add_argument('--nf', default=128, type=int)
    parser.add_argument('--resume', default=False, type=bool)
    parser.add_argument('--beta', default=10, type=float)
    parser.add_argument('--comet', default=False, type=bool)

    args = parser.parse_args()
    return args


class netE_fc(nn.Module):
    def __init__(self, args, datashape, is_training=True):
        super(netE_fc, self).__init__()
        self.dim = dim = args.dim
        self.dshape = datashape
        self.nf = nf = 128
        self.nc = nc = datashape[1]
        self.ng = ng = self.dshape[-1]*self.dshape[-2]*nf
        self.is_training = is_training

        self.linear1 = nn.Linear(ng, nf*4)
        self.linear2 = nn.Linear(nf*4, nf*2)
        self.linear3 = nn.Linear(nf*2, nf)
        self.elu = nn.ELU()
        self.bn1 = nn.BatchNorm2d(nf*4)
        self.bn2 = nn.BatchNorm2d(nf*2)
        self.bn3 = nn.BatchNorm2d(nf)

    def forward(self, x):
        # print ('E in: ', x.shape)
        x = x.view(-1, self.ng)
        if self.is_training:
            z = torch.normal(torch.zeros_like(x.data), std=0.01)
            x.data += z
        x = self.elu(self.bn1(self.linear1(x)))
        x = self.elu(self.bn2(self.linear2(x)))
        x = self.elu(self.bn3(self.linear3(x)))
        # print ('E out: ', x.shape)
        return x


class netG_fc(nn.Module):
    def __init__(self, args, datashape):
        super(netG_fc, self).__init__()
        self.dim = dim = args.dim
        self.dshape = datashape
        self.nf = nf = datashape[-1]
        self.nc = nc = datashape[0]

        self.linear1 = nn.Linear(dim, nf*nf*8)
        self.linear2 = nn.Linear(nf*nf*8, nf*nf*4)
        self.linear3 = nn.Linear(nf*nf*4, nf*nf*2)
        self.linear_out = nn.Linear(nf*nf*2, nf*nf)
        self.elu = nn.ELU()
        self.bn1 = nn.BatchNorm2d(nf*nf*8)
        self.bn2 = nn.BatchNorm2d(nf*nf*4)
        self.bn3 = nn.BatchNorm2d(nf*nf*2)

    def forward(self, x):
        # print ('G in: ', x.shape)
        x = self.elu(self.bn1(self.linear1(x)))
        x = self.elu(self.bn2(self.linear2(x)))
        x = self.elu(self.bn3(self.linear3(x)))
        x = self.linear_out(x)
        x = x.view(-1, self.nf, self.nf)
        # print ('G out: ', x.shape)
        return x


class netG_conv(nn.Module):
    def __init__(self, args, datashape):
        super(netG_conv, self).__init__()
        self.dim = dim = args.dim
        self.dshape = datashape
        self.nf = nf = 128 
        self.nc = nc = 32

        self.linear = nn.Linear(nf, 64*64)
        self.conv1 = nn.Conv2d(1, nc, 3, 2)
        self.conv2 = nn.Conv2d(nc, nc*2, 3, 2)
        self.conv3 = nn.Conv2d(nc*2, nc*4, 3, 2)

        self.elu = nn.ELU()
        self.tanh = nn.Tanh()
        self.bn1 = nn.BatchNorm2d(nc)
        self.bn2 = nn.BatchNorm2d(nc*2)
        # self.bn3 = nn.BatchNorm2d(nc*4)

    def forward(self, x):
        #print ('G in: ', x.shape)
        x = self.elu(self.linear(x))
        x = x.view(-1, 1, 64, 64)
        x = self.elu(self.bn1(self.conv1(x)))
        x = self.elu(self.bn2(self.conv2(x)))
        x = self.conv3(x)
        x = x.view(-1, 128, 7, 7)
        # print ('G out: ', x.shape)
        return x



class netD_fc(nn.Module):
    def __init__(self, args, datashape):
        super(netD_fc, self).__init__()
        self.dim = dim = args.dim
        self.nf = nf = 512
        self.dshape = dshape = datashape

        self.ng = ng = dshape[-1]*dshape[-2]*dshape[0]

        self.linear1 = nn.Linear(ng, nf)
        self.linear2 = nn.Linear(nf, nf)
        self.linear3 = nn.Linear(nf, 1)
        self.elu = nn.ELU()
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        # print ('D in: ', x.shape)
        x = x.view(-1, self.ng)
        x = self.elu(self.linear1(x))
        x = self.elu(self.linear2(x))
        x = self.elu(self.linear3(x))
        x = self.sigmoid(x)
        # print ('D out: ', x.shape)
        return x


def calc_gradient_penalty(args, model, real_data, gen_data):
    batch_size = args.batch_size
    datashape = (128, 256, 7, 7)

    alpha = torch.rand(datashape[0], 1)
    alpha = alpha.expand(datashape[0], int(real_data.nelement()/datashape[0]))
    alpha = alpha.contiguous().view(*datashape).cuda()
    interpolates = alpha * real_data + ((1 - alpha) * gen_data).cuda()
    interpolates = autograd.Variable(interpolates, requires_grad=True)
    disc_interpolates = model(interpolates)
    gradients = autograd.grad(outputs=disc_interpolates,
            inputs=interpolates,
            grad_outputs=torch.ones(disc_interpolates.size()).cuda(),
            create_graph=True,
            retain_graph=True,
            only_inputs=True)[0]
    gradients = gradients.view(gradients.size(0), -1)
    gradient_penalty = ((gradients.norm(2, dim=1) - 1) ** 2).mean() * args.gp
    return gradient_penalty


args = load_args()
shape = (128, 256, 7, 7)

if args.layer == 'conv1':
    netE = netE_fc(args, shape).cuda()
    netG = netG_conv(args, shape).cuda()
    netD = netD_fc(args, shape).cuda()
if args.layer == 'conv2':
    netE = netE_fc(args, shape).cuda()
    netG = netG_conv(args, shape).cuda()
    netD = netD_fc(args, shape).cuda()

train_gen, dev_gen = utils.dataset_iterator(args)
torch.manual_seed(1)

optimizerE = optim.Adam(netE.parameters(), lr=1e-3, betas=(0.5, 0.9))
optimizerG = optim.Adam(netG.parameters(), lr=1e-3, betas=(0.5, 0.9))
optimizerD = optim.Adam(netD.parameters(), lr=1e-3, betas=(0.5, 0.9))
ae_criterion = nn.MSELoss()

one = torch.FloatTensor([1]).cuda()
mone = (one * -1).cuda()
gen = utils.inf_train_gen(train_gen)

for iteration in range(0, 100000):
    start_time = time.time()

    """ Update AE """
    for p in netD.parameters():
        p.requires_grad = False  # to avoid computation
    netG.zero_grad()
    netE.zero_grad()

    _data = next(gen)
    real_data = torch.Tensor(_data).cuda()
    real_data_v = autograd.Variable(real_data)
    encoding  = netE(real_data_v)
    # generate fake layer
    # fake = ops.gen_layer(args, netG, encoding)
    fake = netG(encoding)
    ae_loss = ae_criterion(fake, real_data_v)
    ae_loss.backward(one)
    optimizerE.step()
    optimizerG.step()
     
    """ Update Adversary """
    for p in netD.parameters():  # reset requires_grad
        p.requires_grad = True  # they are set to False below in netG update
    for iter_d in range(5):
        real_data = torch.Tensor(_data).cuda()
        real_data_v = autograd.Variable(real_data)
        netD.zero_grad()
        D_real = netD(real_data_v)
        D_real = D_real.mean()
        D_real.backward(mone)
        noise = torch.randn(args.batch_size, args.dim).cuda()
        noisev = autograd.Variable(noise)
        # fake = ops.gen_layer(args, netG, noisev)
        fake = netG(noisev)
        D_fake = netD(fake)
        D_fake = D_fake.mean()
        D_fake.backward(one)
        # train with gradient penalty

        gradient_penalty = ops.calc_gradient_penalty(args, netD,
                real_data_v.data, fake.data)
        gradient_penalty.backward()

        """ calc classifier loss """
        clf_acc, clf_loss = ops.clf_loss(args, iteration, fake)
        D_cost = D_fake - D_real + gradient_penalty + clf_loss
        Wasserstein_D = D_real - D_fake
        optimizerD.step()

    # Update Generator network
    noise = torch.randn(args.batch_size, args.dim).cuda()
    noisev = autograd.Variable(noise)
    fake = netG(noisev)
    G = netD(fake)
    G = G.mean()
    G.backward(mone)
    G_cost = -G
    optimizerG.step()

    # Write logs and save samples
    save_dir = './plots/{}/{}/{}'.format(args.dataset, args.size, args.layer)
    # Calculate dev loss and generate samples every 100 iters
    if iteration % 10 == 0:
        print ('==> iter: ', iteration)
    if iteration % 100 == 0:
        utils.save_model(netG, optimizerG, iteration,
                'WGAN/{}/Generators/{}G_{}'.format(
                    args.dataset, args.size, iteration))
        utils.save_model(netD, optimizerD, iteration, 
                'WGAN/{}/Discriminators/{}D_{}'.format(
                    args.dataset, args.size, iteration))
        print ("==> saved model instances")
        dev_disc_costs = []
        for params in dev_gen():
            p = torch.Tensor(params).cuda()
            p_v = autograd.Variable(p, volatile=True)
            D = netD(p_v)
            _dev_disc_cost = -D.mean().cpu().data.numpy()
            dev_disc_costs.append(_dev_disc_cost)
        
        path = 'params/sampled/{}/{}/{}'.format(args.dataset, args.size, args.layer)
        if not os.path.exists(path):
            os.makedirs(path)
        z = torch.randn(args.batch_size, args.dim).cuda()
        z = autograd.Variable(z)
        # samples = ops.gen_layer(args, netG, z)
        samples = netG(z)
        acc, loss = ops.clf_loss(args, iteration, samples)
        utils.save_samples(args, samples, iteration, path)
        # acc = utils.generate_samples(iteration, netG, path, args)
        if args.comet:        
            experiment.log_metric('train D cost', D_cost.cpu().data.numpy()[0])
            experiment.log_metric('train G cost', G_cost.cpu().data.numpy()[0])
            experiment.log_metric('AE cost', ae_loss.cpu().data.numpy()[0])
            experiment.log_metric('W1 distance', Wasserstein_D.cpu().data.numpy()[0])
            experiment.log_metric('dev D cost', np.mean(dev_disc_costs))
            experiment.log_metric('{} accuracy'.format(args.dataset), acc)
            experiment.log_metric('{} loss'.format(args.dataset), loss)
        
        print ("****************")
        print('Iter ', iteration, 'Beta ', args.beta)
        print('D cost', D_cost.cpu().data.numpy()[0])
        print('G cost', G_cost.cpu().data.numpy()[0])
        print('AE cost', ae_loss.cpu().data.numpy()[0])
        print('W1 distance', Wasserstein_D.cpu().data.numpy()[0])
        print ('clf accuracy', clf_acc)
        print ('clf loss', clf_loss/args.beta)
        # print sample filter
        print ('filter 1: ', samples[0, 0, :, :])
        print ("****************")


if __name__ == '__main__':
    train()