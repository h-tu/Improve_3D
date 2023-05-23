import argparse
import glob
import importlib
import os
import time

import cv2
import numpy as np
import torch
import torchvision.transforms as transforms
import yaml
from PIL import Image

from edit3d import device, logger
from edit3d.models import deep_sdf
from edit3d.utils.utils import dict2namespace

import datetime
def save(trainer, latent, target, outdir, imname, save_ply=False):
    """Save 2D and 3D modalities after editing"""
    colormesh_filename = os.path.join(outdir, imname)
    # latent_filename = os.path.join(outdir, imname + ".pth")
    pred_sketch_filename = os.path.join(outdir, imname + "_sketch.png")
    pred_3D_filename = os.path.join(outdir, imname + "_3D.png")
    target_filename = os.path.join(outdir, imname + "_target.png")
    shape_code, color_code = latent
    # torch.save(latent, latent_filename)
    if save_ply:
        with torch.no_grad():
            deep_sdf.colormesh.create_mesh(
                trainer.deepsdf_net,
                trainer.colorsdf_net,
                shape_code,
                color_code,
                colormesh_filename,
                N=128,
                max_batch=int(2 ** 18),
            )
    resolution=128
    pred_3d = trainer.render_express(shape_code, color_code, resolution=resolution)
    pred_3d = cv2.cvtColor(pred_3d, cv2.COLOR_RGB2BGR)
    cv2.imwrite(pred_3D_filename, pred_3d)
    pred_sketch = trainer.render_sketch(shape_code)
    save_image(pred_sketch, pred_sketch_filename)
    save_image(target.squeeze().cpu().numpy(), target_filename)


def save_init(trainer, latent, outdir, imname, colormesh=True):
    print(outdir)
    """Save 2D and 3D modalities before editing"""
    colormesh_filename = os.path.join(outdir, imname)
    mesh_filename = os.path.join(outdir, imname + "_wocolor")
    latent_filename = os.path.join(outdir, imname + ".pth")
    pred_3D_filename = os.path.join(outdir, imname + "_3D.png")
    pred_wocolor_3D_filename = os.path.join(outdir, imname + "_wocolor_3D.png")
    shape_code, color_code = latent
    if colormesh:  # generate mesh with surface color from 3D colornet
        with torch.no_grad():
            deep_sdf.colormesh.create_mesh(
                trainer.deepsdf_net,
                trainer.colorsdf_net,
                shape_code.to(device),
                color_code.to(device),
                colormesh_filename,
                N=256,
                max_batch=int(2 ** 18),
            )
    else:  # generate mesh with default color
        with torch.no_grad():
            deep_sdf.mesh.create_mesh(
                trainer.deepsdf_net,
                shape_code.to(device),
                mesh_filename,
                N=256,
                max_batch=int(2 ** 18),
            )
    resolution=128
    torch.save(latent, latent_filename)
    pred_3d_nocolor = trainer.render_express(shape_code, resolution=resolution)
    pred_3d_nocolor = cv2.cvtColor(pred_3d_nocolor, cv2.COLOR_RGB2BGR)
    cv2.imwrite(pred_wocolor_3D_filename, pred_3d_nocolor)
    pred_3d = trainer.render_express(shape_code, color_code, resolution=resolution)
    pred_3d = cv2.cvtColor(pred_3d, cv2.COLOR_RGB2BGR)
    cv2.imwrite(pred_3D_filename, pred_3d)
    pred_sketch = trainer.render_sketch(shape_code)
    pred_sketch_filename = os.path.join(outdir, imname + "_sketch.png")
    save_image(pred_sketch, pred_sketch_filename)


def save_image(image, outname):
    out = np.uint8(image * 255)
    cv2.imwrite(outname, out)


def reconstruct(trainer, target, mask, epoch, trial, gamma, beta):
    temp_shape, temp_color = trainer.get_known_latent(0)
    min_loss = np.inf
    best_latent = None
    for i in range(trial):
        init_shape = torch.randn_like(temp_shape).to(device)
        init_color = torch.randn_like(temp_color).to(device)
        latent, loss = trainer.step_recon_rgb(
            init_shape,
            init_color,
            target,
            mask=mask,
            epoch=epoch,
            gamma=gamma,
            beta=beta,
        )
        if min_loss > loss:
            best_latent = latent[-1]
            min_loss = loss
    return best_latent


def get_mask(source, target):
    mask = 1.0 * ((target - source) != 0)  # 0 means the unmodified region
    return mask

def get_mask_dialated(source,target):
    mask=get_mask(source,target)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2,2))
    print(mask.shape)
    dilate = cv2.dilate(mask.reshape((128,128,1)).numpy(), kernel, iterations=1)
    return torch.Tensor(dilate.reshape((1,128,128)))


def edit(trainer, init_latent, source, target):
    since = time.time()
    init_shape, init_color = init_latent
    mask = get_mask(source, target)
    # print(mask.shape)
    # save_image(mask.reshape((128,128,1)).broadcast_to((128,128,3)), "output/mask_edit_sketch.png")

    latent, loss = trainer.step_edit_sketch(init_shape, target, mask=mask,epoch=300)
    logger.info(f"Editing shape takes {time.time() - since} seconds")
    return latent, init_color  # here the latent contains multiple snapshot


def load_image(path, imsize=128):
    transform = transforms.Compose(
        [
            transforms.Resize((imsize, imsize)),
            transforms.ToTensor(),
        ]
    )
    im = cv2.resize(cv2.imread(path), (128, 128))[:, :, 0]
    data_im = Image.fromarray(im)
    data = transform(data_im)
    return data


def load_image_and_sketch(source_path, editid, prefix,edit_type):
    # print(source_path)
    imagelist = glob.glob(os.path.join(source_path, f"{prefix}*_*.png"))
    print(imagelist)
    if len(imagelist) == 0:
        return None
    source_image = os.path.join(source_path, prefix + ".png")
    source_im = load_image(source_image)
    target_image = imagelist[edit_type]
    target_im = load_image(target_image)
    return {"source": source_im, "target": target_im}


def main(args, cfg):
    # torch.backends.cudnn.benchmark = True
    trainer_lib = importlib.import_module(cfg.trainer.type)
    trainer = trainer_lib.Trainer(cfg, args, device)
    
    # source_dir = os.path.abspath(args.source_dir)
    
    
    pretrained="data/models/airplanes_epoch_2799_iters_156800.pth"
    if args.category == "airplane":
        prefix = "sketch-T-2"
        dir="planes"
    elif args.category == "chair":
        prefix = "sketch-F-2"
        pretrained="data/models/chairs_epoch_2799_iters_280000.pth"
        dir="chairs"
    else:
        logger.error("Only airplane and chair are supported categories")
        raise Exception(f"No such category: {args.category}")
    
    source_dir="examples/edit_via_sketch/"+dir
    outdir="output/edit_via_sketch/out/"
    saveinit=False
    trial=1
    edit_type=args.editid

    trainer.resume_demo(pretrained)
    idx2sid = {}
    for k, v in trainer.sid2idx.items():
        idx2sid[v] = k
    trainer.eval()

    os.makedirs(outdir, exist_ok=True)

    # print(trainer.sid2idx)
    for imname in os.listdir(source_dir):
        print(imname)
        source_path = os.path.join(source_dir, imname)
        logger.info("Edit 3D from %s ..." % source_path)

        for editid in range(1,trial+1):
            logger.debug(editid)
            data = load_image_and_sketch(source_path, editid, prefix,edit_type=edit_type)
            
            if data is None or (imname not in trainer.sid2idx.keys()):
                print("no prior latent")
                continue

            targetdir = outdir
            os.makedirs(targetdir, exist_ok=True)

            source_latent = trainer.get_known_latent(trainer.sid2idx[imname])
            # save init
            if saveinit:
                initdir = os.path.join(targetdir, "init")
                os.makedirs(initdir, exist_ok=True)
                save_init(trainer, source_latent, initdir, imname[:4] +"_"+ str(datetime.datetime.now() ).replace(" ","_").replace(":","_") \
                          + "_init",colormesh=False)

            # editing
            edit_latent, color_code = edit(
                trainer,
                source_latent,
                data["source"],
                data["target"]
            )
            # print(list(edit_latent.shape))
            # if len(list(edit_latent.shape)) > 1:
            if(type(edit_latent)==list):
                edit_latent=edit_latent[-1]
            else: edit_latent=edit_latent[:-1]

            save(
                    trainer,
                    (edit_latent, color_code),
                    data["target"],
                    targetdir,
                    imname[:4]  +"_"+ str(datetime.datetime.now() ).replace(" ","_").replace(":","_"),
                    save_ply=False,
                )
            """
            for iteration, latent_snap in enumerate(edit_latent[-1:]):
                print(targetdir)
                save(
                    trainer,
                    (latent_snap, color_code),
                    data["target"],
                    targetdir,
                    imname[:4]  +"_"+ str(datetime.datetime.now() ).replace(" ","_").replace(":","_")+ f"_{iteration}",
                    save_ply=False,
                )
            """

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reconstruction")
    parser.add_argument("config", type=str, help="The configuration file.")
    parser.add_argument("--pretrained", default=None, type=str, help="pretrained model checkpoint")
    parser.add_argument("--outdir", default=None, type=str, help="path of output")
    parser.add_argument("--category", default="airplane", type=str, help="path of output")
    parser.add_argument("--source_dir", default=None, type=str, help="a text file the lists image")
    parser.add_argument("--trial", default=20, type=int)
    parser.add_argument("--editid", default=0, type=int)
    parser.add_argument("--beta", default=0.5, type=float)
    parser.add_argument("--gamma", default=0.02, type=float)
    parser.add_argument("--epoch", default=10, type=int)




    args = parser.parse_args()

    with open(args.config, "r") as f:
        cfg = yaml.safe_load(f)

    config = dict2namespace(cfg)

    main(args, config)
