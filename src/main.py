import argparse
import os
import re
import sys
import time
from collections import OrderedDict

import numpy as np
import torch
from tensorboardX import SummaryWriter
from torch.optim import SGD, Adam
from torch.utils.data import DataLoader
from torch.utils.data.sampler import SubsetRandomSampler
from torchvision import datasets, transforms
from tqdm import trange

import utils
from sampler import InfiniteSamplerWrapper
from transformer_net import TransformerNet
from vgg import Vgg16

# from PIL import Image
# from PIL import ImageFile

import cv2
import shutil
import subprocess

global args

torch.backends.cudnn.benchmark = True
# Image.MAX_IMAGE_PIXELS = None  # Disable DecompressionBombError
# ImageFile.LOAD_TRUNCATED_IMAGES = True  # Disable OSError: image file is truncated
print("Using device: "+str(torch.cuda.get_device_name(torch.cuda.current_device())))


def check_paths(args):
    try:
        if not os.path.exists(args.save_model_dir):
            os.makedirs(args.save_model_dir)
        if not os.path.exists(args.log_dir):
            os.makedirs(args.log_dir)
        if args.checkpoint_model_dir is not None and not (os.path.exists(args.checkpoint_model_dir)):
            os.makedirs(args.checkpoint_model_dir)
    except OSError as e:
        print(e)
        sys.exit(1)


def loss_fn(features_transformed, features_contents, gram_style, content_weight, style_weight):
    global args
    t_relu = features_transformed[args.layer]
    c_relu = features_contents[args.layer]
    num_egs = t_relu.shape[0]
    content_loss = content_weight * mse_loss(t_relu, c_relu)
    style_loss = 0.0
    for ft_t, gm_s in zip(features_transformed, gram_style):
        gm_t = utils.gram_matrix(ft_t)
        style_loss += mse_loss(gm_t, gm_s[:num_egs, :, :])
    style_loss = style_weight * style_loss
    total_loss = content_loss + style_loss
    return total_loss, content_loss, style_loss


def get_data_loader(args):
    content_transform = transforms.Compose([
        transforms.Resize(args.content_size),
        #transforms.CenterCrop(args.content_size),
        transforms.ToTensor(),
        transforms.Lambda(lambda x: x.mul(255))])
    style_transform = transforms.Compose([
        transforms.Resize((args.style_size, args.style_size)),
        transforms.ToTensor(),
        transforms.Lambda(lambda x: x.mul(255))])

    content_dataset = datasets.ImageFolder(args.content_dataset, content_transform)
    style_dataset = datasets.ImageFolder(args.style_dataset, style_transform)

    content_loader = DataLoader(content_dataset, 
                                batch_size=args.iter_batch_size, 
                                sampler=InfiniteSamplerWrapper(content_dataset),
                                num_workers=args.n_workers)
    style_loader = DataLoader(style_dataset, batch_size=1, 
                              sampler=InfiniteSamplerWrapper(style_dataset),
                              num_workers=args.n_workers)
    query_loader = DataLoader(content_dataset,
                              batch_size=args.iter_batch_size,
                              sampler=InfiniteSamplerWrapper(content_dataset),
                              num_workers=args.n_workers)

    return iter(content_loader), iter(style_loader), iter(query_loader)


def meta_updates(model, dummy_loss, all_meta_grads):
    true_grads = {k: sum(d[k] for d in all_meta_grads) for k in all_meta_grads[0].keys()}
    hooks = []
    for (k, v) in model.named_parameters():
        def get_closure():
            key = k
            def replace_grad(grad):
                return true_grads[key]
            return replace_grad
        hooks.append(v.register_hook(get_closure()))
    optimizer.zero_grad()
    dummy_loss.backward()
    optimizer.step()
    for h in hooks:
        h.remove()    


def train(args):
    """Meta train the model"""

    device = torch.device("cuda" if args.cuda else "cpu")

    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.cuda:
        torch.cuda.manual_seed(args.seed)

    # first move parameters to GPU
    transformer = TransformerNet().to(device)
    vgg = Vgg16(requires_grad=False).to(device)
    global optimizer
    optimizer = Adam(transformer.parameters(), args.meta_lr)
    global mse_loss
    mse_loss = torch.nn.MSELoss()

    content_loader, style_loader, query_loader = get_data_loader(args)

    content_weight = args.content_weight
    style_weight = args.style_weight
    lr = args.lr

    writer = SummaryWriter(args.log_dir)

    for iteration in trange(args.max_iter):
        transformer.train()
        
        # bookkeeping
        # using state_dict causes problems, use named_parameters instead
        all_meta_grads = []
        avg_train_c_loss = 0.0
        avg_train_s_loss = 0.0
        avg_train_loss = 0.0
        avg_eval_c_loss = 0.0
        avg_eval_s_loss = 0.0
        avg_eval_loss = 0.0

        contents = content_loader.next()[0].to(device)
        features_contents = vgg(utils.normalize_batch(contents))
        querys = query_loader.next()[0].to(device)
        features_querys = vgg(utils.normalize_batch(querys))

        # learning rate scheduling
        lr = args.lr / (1.0 + iteration * 2.5e-5)
        meta_lr = args.meta_lr / (1.0 + iteration * 2.5e-5)
        for param_group in optimizer.param_groups:
            param_group['lr'] = meta_lr

        for i in range(args.meta_batch_size):
            # sample a style
            style = style_loader.next()[0].to(device)
            style = style.repeat(args.iter_batch_size, 1, 1, 1)
            features_style = vgg(utils.normalize_batch(style))
            gram_style = [utils.gram_matrix(y) for y in features_style]

            fast_weights = OrderedDict((name, param) for (name, param) in transformer.named_parameters() if re.search(r'in\d+\.', name))
            for j in range(args.meta_step):
                # run forward transformation on contents
                transformed = transformer(contents, fast_weights)

                # compute loss
                features_transformed = vgg(utils.standardize_batch(transformed))
                loss, c_loss, s_loss = loss_fn(features_transformed, features_contents, gram_style, content_weight, style_weight)

                # compute grad
                grads = torch.autograd.grad(loss, fast_weights.values(), create_graph=True)

                # update fast weights
                fast_weights = OrderedDict((name, param - lr * grad) for ((name, param), grad) in zip(fast_weights.items(), grads))
            
            avg_train_c_loss += c_loss.item()
            avg_train_s_loss += s_loss.item()
            avg_train_loss += loss.item()

            # run forward transformation on querys
            transformed = transformer(querys, fast_weights)
            
            # compute loss
            features_transformed = vgg(utils.standardize_batch(transformed))
            loss, c_loss, s_loss = loss_fn(features_transformed, features_querys, gram_style, content_weight, style_weight)
            
            grads = torch.autograd.grad(loss / args.meta_batch_size, transformer.parameters())
            all_meta_grads.append({name: g for ((name, _), g) in zip(transformer.named_parameters(), grads)})

            avg_eval_c_loss += c_loss.item()
            avg_eval_s_loss += s_loss.item()
            avg_eval_loss += loss.item()
        
        writer.add_scalar("Avg_Train_C_Loss", avg_train_c_loss / args.meta_batch_size, iteration + 1)
        writer.add_scalar("Avg_Train_S_Loss", avg_train_s_loss / args.meta_batch_size, iteration + 1)
        writer.add_scalar("Avg_Train_Loss", avg_train_loss / args.meta_batch_size, iteration + 1)
        writer.add_scalar("Avg_Eval_C_Loss", avg_eval_c_loss / args.meta_batch_size, iteration + 1)
        writer.add_scalar("Avg_Eval_S_Loss", avg_eval_s_loss / args.meta_batch_size, iteration + 1)
        writer.add_scalar("Avg_Eval_Loss", avg_eval_loss / args.meta_batch_size, iteration + 1)

        # compute dummy loss to refresh buffer
        transformed = transformer(querys)
        features_transformed = vgg(utils.standardize_batch(transformed))
        dummy_loss, _, _ = loss_fn(features_transformed, features_querys, gram_style, content_weight, style_weight)

        meta_updates(transformer, dummy_loss, all_meta_grads)

        if args.checkpoint_model_dir is not None and (iteration + 1) % args.checkpoint_interval == 0:
            transformer.eval().cpu()
            ckpt_model_filename = "iter_" + str(iteration + 1) + ".pth"
            ckpt_model_path = os.path.join(args.checkpoint_model_dir, ckpt_model_filename)
            torch.save(transformer.state_dict(), ckpt_model_path)
            transformer.to(device).train()

    # save model
    transformer.eval().cpu()
    save_model_filename = "Final_iter_" + str(args.max_iter) + "_" + \
                          str(args.content_weight) + "_" + \
                          str(args.style_weight) + "_" + \
                          str(args.lr) + "_" + \
                          str(args.meta_lr) + "_" + \
                          str(args.meta_batch_size) + "_" + \
                          str(args.meta_step) + "_" + \
                          time.ctime() + ".pth"
    save_model_path = os.path.join(args.save_model_dir, save_model_filename)
    torch.save(transformer.state_dict(), save_model_path)

    print("Done, trained model saved at {}".format(save_model_path))


def fast_train(args):
    """Fast training"""

    device = torch.device("cuda" if args.cuda else "cpu")

    transformer = TransformerNet().to(device)
    if args.model:
        transformer.load_state_dict(torch.load(args.model))
    vgg = Vgg16(requires_grad=False).to(device)
    global mse_loss
    mse_loss = torch.nn.MSELoss()

    content_weight = args.content_weight
    style_weight = args.style_weight
    lr = args.lr

    content_transform = transforms.Compose([
        transforms.Resize(args.content_size),
        transforms.CenterCrop(args.content_size),
        transforms.ToTensor(),
        transforms.Lambda(lambda x: x.mul(255))])
    content_dataset = datasets.ImageFolder(args.content_dataset, content_transform)
    content_loader = DataLoader(content_dataset, 
                                batch_size=args.iter_batch_size, 
                                sampler=InfiniteSamplerWrapper(content_dataset),
                                num_workers=args.n_workers)
    content_loader = iter(content_loader)
    style_transform = transforms.Compose([
            transforms.Resize((args.style_size, args.style_size)),
            transforms.ToTensor(),
            transforms.Lambda(lambda x: x.mul(255))])

    style_image = utils.load_image(args.style_image)
    style_image = style_transform(style_image)
    style_image = style_image.unsqueeze(0).to(device)
    features_style = vgg(utils.normalize_batch(style_image.repeat(args.iter_batch_size, 1, 1, 1)))
    gram_style = [utils.gram_matrix(y) for y in features_style]

    if args.only_in:
        optimizer = Adam([param for (name, param) in transformer.named_parameters() if "in" in name], lr=lr)
    else:
        optimizer = Adam(transformer.parameters(), lr=lr)
    
    running_loss = 0.0
    count=0
    t = trange(args.update_step, desc='Loss: N/A', leave=True)
    for i in t:
        if (i > 0) and (i % 5 == 0):
            t.set_description("Loss: " + str(running_loss/i))
            t.refresh()

        contents = content_loader.next()[0].to(device)
        features_contents = vgg(utils.normalize_batch(contents))

        transformed = transformer(contents)
        features_transformed = vgg(utils.standardize_batch(transformed))
        loss, c_loss, s_loss = loss_fn(features_transformed, features_contents, gram_style, content_weight, style_weight)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        if (args.test > 0) and (i % args.test == 0):
            torch.save(transformer.state_dict(), "drive/IMP_STYLE/RE/"+str(count)+".pth")
            count=count+1
        
        running_loss += loss.item()
        t.refresh()
    
    print("End Loss: " + str(running_loss/args.update_step))
    running_loss = 0.0
    
    # save model
    transformer.eval().cpu()
    style_name = os.path.basename(args.style_image).split(".")[0]
    save_model_filename = style_name + ".pth"
    save_model_path = os.path.join(args.save_model_dir, save_model_filename)
    torch.save(transformer.state_dict(), save_model_path)


def test(args):
    """Stylize a content image"""

    device = torch.device("cuda" if args.cuda else "cpu")

    transformer = TransformerNet().to(device)
    if args.model:
        transformer.load_state_dict(torch.load(args.model))

    content_transform = transforms.Compose([
        transforms.Resize(args.content_size),
        #transforms.CenterCrop(args.content_size),
        transforms.ToTensor(),
        transforms.Lambda(lambda x: x.mul(255))])
    content_image = utils.load_image(args.content_image)
    content_image = content_transform(content_image)
    content_image = content_image.unsqueeze(0).to(device)
    output = transformer(content_image).cpu().detach()
    utils.save_image(args.output_image, output[0] * 255)
    if args.original_colors:
        source = cv2.imread(args.content_image)
        target = cv2.imread(args.output_image)
        if args.color_convert_type == 'yuv':
          cvt_type = cv2.COLOR_BGR2YUV
          inv_cvt_type = cv2.COLOR_YUV2BGR
        elif args.color_convert_type == 'ycrcb':
          cvt_type = cv2.COLOR_BGR2YCR_CB
          inv_cvt_type = cv2.COLOR_YCR_CB2BGR
        elif args.color_convert_type == 'luv':
          cvt_type = cv2.COLOR_BGR2LUV
          inv_cvt_type = cv2.COLOR_LUV2BGR
        elif args.color_convert_type == 'lab':
          cvt_type = cv2.COLOR_BGR2LAB
          inv_cvt_type = cv2.COLOR_LAB2BGR
        content_cvt = cv2.cvtColor(source, cvt_type)
        stylized_cvt = cv2.cvtColor(target, cvt_type)
        c1, _, _ = cv2.split(stylized_cvt)
        _, c2, c3 = cv2.split(content_cvt)
        merged = cv2.merge((c1, c2, c3))
        dst = cv2.cvtColor(merged, inv_cvt_type).astype(np.float32)
        cv2.imwrite(args.output_image, dst)

def video(args):
    """Stylize a content video"""

    device = torch.device("cuda" if args.cuda else "cpu")

    transformer = TransformerNet().to(device)
    if args.model:
        transformer.load_state_dict(torch.load(args.model))
    
    content_transform = transforms.Compose([
        transforms.Resize(args.content_size),
        #transforms.CenterCrop(args.content_size),
        transforms.ToTensor(),
        transforms.Lambda(lambda x: x.mul(255))])

    os.mkdir("recon")

    vidcap = cv2.VideoCapture(args.content_video)

    # Find OpenCV version
    (major_ver, minor_ver, subminor_ver) = (cv2.__version__).split('.')

    if int(major_ver)  < 3 :
        fps = vidcap.get(cv2.cv.CV_CAP_PROP_FPS)
    else :
        fps = vidcap.get(cv2.CAP_PROP_FPS)

    success,image = vidcap.read()
    count = 0
    while success:
        if count==0:
            cv2.imwrite("recon/frame0.jpg", image)     # save frame as JPEG file      
        success,image = vidcap.read()
        count += 1

    vidcap.release()

    print("Found " + str(count) + " frames")

    frame = cv2.imread("recon/frame0.jpg")
    height, width, layers = frame.shape
    os.remove("recon/frame0.jpg")

    fourcc = cv2.VideoWriter_fourcc(*'XVID')
    video = cv2.VideoWriter("recon/NOAUD.avi", fourcc, fps, (width,height)) 
    vidcap = cv2.VideoCapture(args.content_video)

    for i in trange(count):
        i=str(i)
        success,image = vidcap.read()
        if success==True:
            cv2.imwrite("recon/frame"+i+".jpg", image) 
            content_image = utils.load_image("recon/frame"+i+".jpg")
            source = cv2.imread("recon/frame"+i+".jpg")
            content_image = content_transform(content_image)
            content_image = content_image.unsqueeze(0).to(device)
            output = transformer(content_image).cpu().detach()
            utils.save_image("recon/frame"+i+".jpg", output[0] * 255)
            target = cv2.imread("recon/frame"+i+".jpg")
            if args.original_colors:
                if args.color_convert_type == 'yuv':
                  cvt_type = cv2.COLOR_BGR2YUV
                  inv_cvt_type = cv2.COLOR_YUV2BGR
                elif args.color_convert_type == 'ycrcb':
                  cvt_type = cv2.COLOR_BGR2YCR_CB
                  inv_cvt_type = cv2.COLOR_YCR_CB2BGR
                elif args.color_convert_type == 'luv':
                  cvt_type = cv2.COLOR_BGR2LUV
                  inv_cvt_type = cv2.COLOR_LUV2BGR
                elif args.color_convert_type == 'lab':
                  cvt_type = cv2.COLOR_BGR2LAB
                  inv_cvt_type = cv2.COLOR_LAB2BGR
                content_cvt = cv2.cvtColor(source, cvt_type)
                stylized_cvt = cv2.cvtColor(target, cvt_type)
                c1, _, _ = cv2.split(stylized_cvt)
                _, c2, c3 = cv2.split(content_cvt)
                merged = cv2.merge((c1, c2, c3))
                dst = cv2.cvtColor(merged, inv_cvt_type).astype(np.float32)
                cv2.imwrite("recon/frame"+i+".jpg", dst)
            video.write(cv2.imread("recon/frame"+i+".jpg"))

    vidcap.release()
    cv2.destroyAllWindows()
    video.release()

    #Extract audio
    subprocess.call(['ffmpeg', '-i', args.content_video, '-f', 'mp3', '-ab', '192000', '-vn', 'recon/v_aud.mp3'])
    subprocess.call(['ffmpeg', '-i', "recon/NOAUD.avi", '-i', 'recon/v_aud.mp3', '-vcodec', 'x265', '-crf', '24', '-map', '0:0', '-map', '1:0', '-c:v', 'copy', '-c:a', 'copy', args.output_video])

    shutil.rmtree("recon")


def main():
    main_arg_parser = argparse.ArgumentParser(description="parser for meta-style")
    subparsers = main_arg_parser.add_subparsers(title="subcommands", dest="subcommand")

    train_arg_parser = subparsers.add_parser("train", help="parser for training")
    train_arg_parser.add_argument("--max-iter", type=int, default=100000,
                                  help="number of training iterations, large enough to traverse the style " 
                                       "dataset for several epochs")
    train_arg_parser.add_argument("--iter-batch-size", type=int, default=4,
                                  help="batch size for each style training iteration")
    train_arg_parser.add_argument("--meta-batch-size", type=int, default=4,
                                  help="meta batch size, the number of styles during each meta update")
    train_arg_parser.add_argument("--meta-step", type=int, default=1,
                                  help="the number of inner loop steps to take")
    train_arg_parser.add_argument("--content-dataset", type=str, required=True,
                                  help="path to content dataset, the path should point to a folder "
                                       "containing another folder with all content images")
    train_arg_parser.add_argument("--style-dataset", type=str, required=True,
                                  help="path to style dataset, the path should point to a folder "
                                       "containing another folder with all style images")
    train_arg_parser.add_argument("--save-model-dir", type=str, default="./experiments/save",
                                  help="path to folder where trained model will be saved.")
    train_arg_parser.add_argument("--checkpoint-model-dir", type=str, default="./experiments/ckpt",
                                  help="path to folder where checkpoints of trained models will be saved")
    train_arg_parser.add_argument("--log-dir", type=str, default="./experiments/logs",
                                  help="path to the folder where log events are stored")
    train_arg_parser.add_argument("--content-size", type=int, default=256,
                                  help="size of content images")
    train_arg_parser.add_argument("--style-size", type=int, default=256,
                                  help="size of style image")
    train_arg_parser.add_argument("--cuda", type=int, required=True,
                                  help="set it to 1 for running on GPU, 0 for CPU")
    train_arg_parser.add_argument("--seed", type=int, default=12345,
                                  help="random seed for training")
    train_arg_parser.add_argument("--content-weight", type=float, default=1,
                                  help="weight for content-loss")
    train_arg_parser.add_argument("--style-weight", type=float, default=1e5,
                                  help="weight for style-loss")
    train_arg_parser.add_argument("--lr", type=float, default=1e-5,
                                  help="learning rate")
    train_arg_parser.add_argument("--meta-lr", type=float, default=1e-5,
                                  help="learning rate of meta update")
    train_arg_parser.add_argument("--checkpoint-interval", type=int, default=2000,
                                  help="number of iterations after which a checkpoint of the trained model will be created")
    train_arg_parser.add_argument("--n-workers", type=int, default=16,
                                  help="number of workers for each data loader")

    fast_arg_parser = subparsers.add_parser("fast", help="parser for fast training")
    fast_arg_parser.add_argument("--content-dataset", type=str, required=True,
                                 help="path to a content dataset for fast training")
    fast_arg_parser.add_argument("--content-size", type=int, default=256,
                                 help="factor for scaling the content image")
    fast_arg_parser.add_argument("--style-image", type=str, required=True,
                                 help="style image path")
    fast_arg_parser.add_argument("--style-size", type=int, default=512,
                                 help="factor for resizing the style image")
    fast_arg_parser.add_argument("--model", type=str, default=None,
                                 help="path to the pretrained model")
    fast_arg_parser.add_argument("--cuda", type=int, required=True,
                                 help="set it to 1 to run on CUDA")
    fast_arg_parser.add_argument("--update-step", type=int, default=200,
                                 help="training steps for the new model")
    fast_arg_parser.add_argument("--content-weight", type=float, default=1,
                                 help="weight for the content loss")
    fast_arg_parser.add_argument("--style-weight", type=float, default=1e5,
                                 help="weight for the style loss")
    fast_arg_parser.add_argument("--lr", type=float, default=1e-3,
                                 help="learning rate for the model")
    fast_arg_parser.add_argument("--iter-batch-size", type=int, default=4,
                                 help="batch size for fast training")
    fast_arg_parser.add_argument("--n-workers", type=int, default=16,
                                 help="number of workers for data loading")
    fast_arg_parser.add_argument("--save-model-dir", type=str, default="./experiments/save",
                                  help="path to folder where trained model will be saved.")
    fast_arg_parser.add_argument("--only-in", type=int, default=0,
                                 help="update IN layers only if set to 1")
    fast_arg_parser.add_argument("--layer", type=int, default=1,
                                 help="layer type, either r1, r2, r3, r4, or r5")
    fast_arg_parser.add_argument("--test", type=int, default=0,
                                 help="to test or not to test... will save images of every x iterations")
    
    test_arg_parser = subparsers.add_parser("test", help="parser for testing")
    test_arg_parser.add_argument("--content-image", type=str, required=True,
                                 help="path to content image you want to stylize")
    test_arg_parser.add_argument("--content-size", type=int, default=512,
                                 help="factor for scaling down the content image")
    test_arg_parser.add_argument("--output-image", type=str, required=True,
                                 help="path for saving the output image")
    test_arg_parser.add_argument("--model", type=str, default=None,
                                 help="saved model to be used for stylizing the image. Should end in .pth - PyTorch path is used")
    test_arg_parser.add_argument("--cuda", type=int, required=True,
                                 help="set it to 1 for running on GPU, 0 for CPU")
    test_arg_parser.add_argument("--original-colors", type=int, default=0,
                                 help="set it to 0 for no original colors, 1 for original colors")
    test_arg_parser.add_argument('--color_convert_type', type=str, default='yuv',
                                 choices=['yuv', 'ycrcb', 'luv', 'lab'], 
                                 help='Color space for conversion to original colors')

    video_arg_parser = subparsers.add_parser("video", help="parser for video eval")
    video_arg_parser.add_argument("--content-video", type=str, required=True,
                                 help="path to content video you want to stylize")
    video_arg_parser.add_argument("--content-size", type=int, default=512,
                                 help="factor for scaling down the content video")
    video_arg_parser.add_argument("--output-video", type=str, required=True,
                                 help="path for saving the output video")
    video_arg_parser.add_argument("--model", type=str, default=None,
                                 help="saved model to be used for stylizing the video. Should end in .pth - PyTorch path is used")
    video_arg_parser.add_argument("--cuda", type=int, required=True,
                                 help="set it to 1 for running on GPU, 0 for CPU")
    video_arg_parser.add_argument("--original-colors", type=int, default=0,
                                 help="set it to 0 for no original colors, 1 for original colors")
    video_arg_parser.add_argument('--color_convert_type', type=str, default='yuv',
                                 choices=['yuv', 'ycrcb', 'luv', 'lab'], 
                                 help='Color space for conversion to original colors')

    global args
    args = main_arg_parser.parse_args()

    if args.subcommand is None:
        print("ERROR: specify either train or fast train")
        sys.exit(1)
    if args.cuda and not torch.cuda.is_available():
        print("ERROR: cuda is not available, try running on CPU")
        sys.exit(1)

    if args.subcommand == "train":
        check_paths(args)
        train(args)
    elif args.subcommand == "fast":
        fast_train(args)
    elif args.subcommand == "test":
        test(args)
    elif args.subcommand == "video":
        video(args)
    else:
        raise ValueError("Unknown program type")


if __name__ == "__main__":
    main()
