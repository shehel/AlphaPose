# -----------------------------------------------------
# Copyright (c) Shanghai Jiao Tong University. All rights reserved.
# Written by Jiefeng Li (jeff.lee.sjtu@gmail.com)
# -----------------------------------------------------

import torch
import torch.utils.data
import torch.nn as nn

from lr_finder import LRFinder

from utils.dataset import cocot as coco
from opt import opt
from tqdm import tqdm
from models.FastPose import createModel
from utils.eval import DataLogger, accuracy
from utils.img import flip, shuffleLR
from evaluation import prediction

from models.layers.DUC import DUC
from models.layers.SE_Resnet import SEResnet

from tensorboardX import SummaryWriter
import os


def train(train_loader, m, criterion, optimizer, writer, n_gpu):
    lossLogger = DataLogger()
    accLogger = DataLogger()
    m.train()

    train_loader_desc = tqdm(train_loader)

    for i, (inps, labels, setMask, imgset) in enumerate(train_loader_desc):
        inps = inps.cuda().requires_grad_()
        labels = labels.cuda()
        setMask = setMask.cuda()
        out = m(inps)

        loss = criterion(out.mul(setMask), labels)
        if n_gpu > 1:
            loss = loss.mean()
        acc = accuracy(out.data.mul(setMask), labels.data, train_loader.dataset)

        accLogger.update(acc[0], inps.size(0))
        lossLogger.update(loss.item(), inps.size(0))

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        opt.trainIters += 1
        # Tensorboard
        writer.add_scalar(
            'Train/Loss', lossLogger.avg, opt.trainIters)
        writer.add_scalar(
            'Train/Acc', accLogger.avg, opt.trainIters)

        # TQDM
        train_loader_desc.set_description(
            'loss: {loss:.8f} | acc: {acc:.2f}'.format(
                loss=lossLogger.avg,
                acc=accLogger.avg * 100)
        )

    train_loader_desc.close()

    return lossLogger.avg, accLogger.avg


def valid(val_loader, m, criterion, optimizer, writer):
    lossLogger = DataLogger()
    accLogger = DataLogger()
    m.eval()

    val_loader_desc = tqdm(val_loader)

    for i, (inps, labels, setMask, imgset) in enumerate(val_loader_desc):
        inps = inps.cuda()
        labels = labels.cuda()
        setMask = setMask.cuda()

        with torch.no_grad():
            out = m(inps)

            loss = criterion(out.mul(setMask), labels)

            flip_out = m(flip(inps))
            flip_out = flip(shuffleLR(flip_out, val_loader.dataset))

            out = (flip_out + out) / 2

        acc = accuracy(out.mul(setMask), labels, val_loader.dataset)

        lossLogger.update(loss.item(), inps.size(0))
        accLogger.update(acc[0], inps.size(0))

        opt.valIters += 1

        # Tensorboard
        writer.add_scalar(
            'Valid/Loss', lossLogger.avg, opt.valIters)
        writer.add_scalar(
            'Valid/Acc', accLogger.avg, opt.valIters)

        val_loader_desc.set_description(
            'loss: {loss:.8f} | acc: {acc:.2f}'.format(
                loss=lossLogger.avg,
                acc=accLogger.avg * 100)
        )

    val_loader_desc.close()

    return lossLogger.avg, accLogger.avg


def main():
    n_gpu = torch.cuda.device_count();
    # Model Initialize
    m = createModel()
    if opt.loadModel:
        print('Loading Model from {}'.format(opt.loadModel))
        m.load_state_dict(torch.load(opt.loadModel))
        if not os.path.exists("../exp/{}/{}".format(opt.dataset, opt.expID)):
            try:
                os.mkdir("../exp/{}/{}".format(opt.dataset, opt.expID))
            except FileNotFoundError:
                os.mkdir("../exp/{}".format(opt.dataset))
                os.mkdir("../exp/{}/{}".format(opt.dataset, opt.expID))
    else:
        print('Create new model')
        if not os.path.exists("../exp/{}/{}".format(opt.dataset, opt.expID)):
            try:
                os.mkdir("../exp/{}/{}".format(opt.dataset, opt.expID))
            except FileNotFoundError:
                os.mkdir("../exp/{}".format(opt.dataset))
                os.mkdir("../exp/{}/{}".format(opt.dataset, opt.expID))
    for param in m.parameters():
        param.requires_grad = False
    if opt.nClasses != opt.oClasses:
        m.conv_out = nn.Conv2d(
            128, opt.nClasses, kernel_size=3, stride=1, padding=1)
    
    m = m.cuda()

    criterion = torch.nn.MSELoss().cuda()

    if opt.optMethod == 'rmsprop':
        optimizer = torch.optim.RMSprop(m.conv_out.parameters(),
                                        lr=opt.LR,
                                        momentum=opt.momentum,
                                        weight_decay=opt.weightDecay)
    elif opt.optMethod == 'adam':
        optimizer = torch.optim.Adam(
            m.parameters(),
            lr=opt.LR
        )
    else:
        raise Exception

    writer = SummaryWriter(
        '.tensorboard/{}/{}'.format(opt.dataset, opt.expID))

    # Prepare Dataset
    if opt.dataset == 'coco':
        train_dataset = coco.Mscoco(train=True)
        val_dataset = coco.Mscoco(train=False)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=opt.trainBatch, shuffle=True, num_workers=opt.nThreads, pin_memory=True)

    val_loader = torch.utils.data.DataLoader(
        val_dataset, batch_size=opt.validBatch, shuffle=False, num_workers=opt.nThreads, pin_memory=True)

    # Model Transfer
    print("Training beginning on: ", n_gpu)
    m = torch.nn.DataParallel(m).cuda()
    if opt.lr_find:
        lr_finder = LRFinder(m, optimizer, criterion, device="cuda")
        lr_finder.range_test(train_loader, end_lr=100, num_iter=100, step_mode="exp", diverge_th=5)
        lr_finder.plot()
    # Start Training
    for i in range(opt.nEpochs):
        opt.epoch = i

        print('############# Starting Epoch {} #############'.format(opt.epoch))
        loss, acc = train(train_loader, m, criterion, optimizer, writer, n_gpu)

        print('Train-{idx:d} epoch | loss:{loss:.8f} | acc:{acc:.4f}'.format(
            idx=opt.epoch,
            loss=loss,
            acc=acc
        ))

        opt.acc = acc
        opt.loss = loss
        m_dev = m.module
        if i % opt.snapshot == 0:
            torch.save(
                m_dev.state_dict(), '../exp/{}/{}/model_{}.pth'.format(opt.dataset, opt.expID, opt.epoch))
            torch.save(
                opt, '../exp/{}/{}/option.pth'.format(opt.dataset, opt.expID, opt.epoch))
            torch.save(
                optimizer, '../exp/{}/{}/optimizer.pth'.format(opt.dataset, opt.expID))

        loss, acc = valid(val_loader, m, criterion, optimizer, writer)

        print('Valid-{idx:d} epoch | loss:{loss:.8f} | acc:{acc:.4f}'.format(
            idx=i,
            loss=loss,
            acc=acc
        ))

        '''
        if opt.dataset != 'mpii':
            with torch.no_grad():
                mAP, mAP5 = prediction(m)

            print('Prediction-{idx:d} epoch | mAP:{mAP:.3f} | mAP0.5:{mAP5:.3f}'.format(
                idx=i,
                mAP=mAP,
                mAP5=mAP5
            ))
        '''
    writer.close()


if __name__ == '__main__':
    main()
