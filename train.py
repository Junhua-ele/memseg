import time
import json
import os
import mlflow
import logging

import torch
import torch.nn.functional as F
import numpy as np
from typing import List
from sklearn.metrics import roc_auc_score
from torchmetrics.functional import precision_recall_curve
from metrics import compute_pro, trapezoid
from omegaconf import OmegaConf

import cv2 as cv

_logger = logging.getLogger('train')


class AverageMeter:
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def training(cfg, model, trainloader, validloader, criterion, optimizer, scheduler, num_training_steps: int = 1000,
             loss_weights: List[float] = [0.6, 0.4],
             log_interval: int = 1, eval_interval: int = 1, savedir: str = None, use_mlflow: bool = False,
             device: str = 'cpu') -> dict:
    cfg = cfg
    batch_time_m = AverageMeter()
    data_time_m = AverageMeter()
    losses_m = AverageMeter()
    l1_losses_m = AverageMeter()
    focal_losses_m = AverageMeter()

    # criterion
    l1_criterion, focal_criterion = criterion
    l1_weight, focal_weight = loss_weights

    # set train mode
    model.train()

    # set optimizer
    optimizer.zero_grad()

    # training
    best_score = 0
    step = 0
    train_mode = True

    if use_mlflow:
        mlflow.start_run()
        mlflow.log_params(OmegaConf.to_container(cfg))

    while train_mode:

        end = time.time()
        for inputs, masks, targets in trainloader:
            # batch
            inputs, masks, targets = inputs.to(device), masks.to(device), targets.to(device)

            data_time_m.update(time.time() - end)

            # predict
            outputs = model(inputs)
            outputs = F.softmax(outputs, dim=1)
            l1_loss = l1_criterion(outputs[:, 1, :], masks)
            focal_loss = focal_criterion(outputs, masks)
            loss = (l1_weight * l1_loss) + (focal_weight * focal_loss)

            loss.backward()

            # update weight
            optimizer.step()
            optimizer.zero_grad()

            # log loss
            l1_losses_m.update(l1_loss.item())
            focal_losses_m.update(focal_loss.item())
            losses_m.update(loss.item())

            batch_time_m.update(time.time() - end)

            if use_mlflow:
                mlflow.log_metrics({
                    'lr': optimizer.param_groups[0]['lr'],
                    'train_focal_loss': focal_losses_m.val,
                    'train_l1_loss': l1_losses_m.val,
                    'train_loss': losses_m.val
                }, step=step)

            if (step + 1) % log_interval == 0 or step == 0:
                _logger.info('TRAIN [{:>4d}/{}] '
                             'Loss: {loss.val:>6.4f} ({loss.avg:>6.4f}) '
                             'L1 Loss: {l1_loss.val:>6.4f} ({l1_loss.avg:>6.4f}) '
                             'Focal Loss: {focal_loss.val:>6.4f} ({focal_loss.avg:>6.4f}) '
                             'LR: {lr:.3e} '
                             'Time: {batch_time.val:.3f}s, {rate:>7.2f}/s ({batch_time.avg:.3f}s, {rate_avg:>7.2f}/s) '
                             'Data: {data_time.val:.3f} ({data_time.avg:.3f})'.format(
                    step + 1, num_training_steps,
                    loss=losses_m,
                    l1_loss=l1_losses_m,
                    focal_loss=focal_losses_m,
                    lr=optimizer.param_groups[0]['lr'],
                    batch_time=batch_time_m,
                    rate=inputs.size(0) / batch_time_m.val,
                    rate_avg=inputs.size(0) / batch_time_m.avg,
                    data_time=data_time_m))

            if ((step + 1) % eval_interval == 0 and step != 0) or (step + 1) == num_training_steps:
                eval_metrics = evaluate(
                    model=model,
                    dataloader=validloader,
                    device=device,
                    save_dir=savedir
                )
                model.train()

                eval_log = dict([(f'eval_{k}', v) for k, v in eval_metrics.items()])

                if use_mlflow:
                    mlflow.log_metrics(eval_log, step=step)

                # checkpoint
                eval_values = [v.cpu() if isinstance(v, torch.Tensor) else v for v in eval_metrics.values()]
                if best_score < np.mean(list(eval_values)):
                    # save best score
                    state = {'best_step': step}

                    eval_log_serializable = {
                        k: v.item() if isinstance(v, torch.Tensor) and v.numel() == 1 else v.cpu().numpy() if isinstance(v, torch.Tensor) else v
                        for k, v in eval_log.items()
                    }

                    state.update(eval_log_serializable)
                    json.dump(state, open(os.path.join(savedir, 'best_score.json'), 'w'), indent='\t')

                    # save best model
                    torch.save(model.state_dict(), os.path.join(savedir, f'best_model.pt'))

                    _logger.info(
                        'Best Score {0:.3%} to {1:.3%}'.format(best_score, np.mean(list(eval_values))))

                    best_score = np.mean(list(eval_values))

            # scheduler
            if scheduler:
                scheduler.step()

            end = time.time()

            step += 1

            if step == num_training_steps:
                train_mode = False
                break

    # print best score and step
    # _logger.info('Best Metric: {0:.3%} (step {1:})'.format(best_score, state['best_step']))

    # save latest model
    torch.save(model.state_dict(), os.path.join(savedir, f'latest_model.pt'))
    if use_mlflow:
        mlflow.end_run()

    # save latest score
    eval_log_serializable = {
                        k: v.item() if isinstance(v, torch.Tensor) and v.numel() == 1 else v.cpu().numpy() if isinstance(v, torch.Tensor) else v
                        for k, v in eval_log.items()
                    }
    state = {'latest_step': step}
    state.update(eval_log_serializable)
    json.dump(state, open(os.path.join(savedir, 'latest_score.json'), 'w'), indent='\t')


def evaluate(model, dataloader, device: str = 'cpu', save_dir: str = None):
    # targets and outputs
    image_targets = []
    image_masks = []
    anomaly_score = []
    anomaly_map = []

    model.eval()
    with torch.no_grad():
        for idx, (inputs, masks, targets) in enumerate(dataloader):
            inputs, masks, targets = inputs.to(device), masks.to(device), targets.to(device)

            # predict
            outputs = model(inputs)
            outputs = F.softmax(outputs, dim=1)
            anomaly_score_i = torch.topk(torch.flatten(outputs[:, 1, :], start_dim=1), 100)[0].mean(dim=1)

            # stack targets and outputs
            image_targets.extend(targets.cpu().tolist())
            image_masks.extend(masks.cpu().numpy())

            anomaly_score.extend(anomaly_score_i.cpu().tolist())
            anomaly_map.extend(outputs[:, 1, :].cpu().numpy())

            if save_dir is not None:
                file_path = dataloader.dataset.file_list[idx]
                save_path = os.path.join(save_dir, f'combined_sample_{idx}.png')
                create_heatmaps(
                    input_image=inputs[0].cpu(),
                    anomaly_map=outputs[0, 1, :].cpu().numpy(),
                    ground_truth=masks[0].cpu().numpy(),
                    save_path=save_path,
                    file_path=file_path
                )

    # metrics    
    image_masks = np.array(image_masks)
    anomaly_map = np.array(anomaly_map)

    auroc_image = roc_auc_score(image_targets, anomaly_score)
    best_f1, best_threshold = compute_F1(image_targets, anomaly_score, device)

    auroc_pixel = roc_auc_score(image_masks.reshape(-1).astype(int), anomaly_map.reshape(-1))
    all_fprs, all_pros = compute_pro(
        anomaly_maps=anomaly_map,
        ground_truth_maps=image_masks
    )
    aupro = trapezoid(all_fprs, all_pros)

    metrics = {
        'AUROC-image': auroc_image,
        'Best-F1-score': best_f1,
        'Best-threshold': best_threshold,
    }

    _logger.info('TEST: AUROC-image: %.3f%% | Best-F1-score: %.3f%% | Best-threshold: %.3f%%' %
                 (metrics['AUROC-image'], metrics['Best-F1-score'], metrics['Best-threshold']))

    return metrics


def compute_F1(image_targets, anomaly_score, device):
    image_targets_tensor = torch.tensor(image_targets, device=device)
    anomaly_score_tensor = torch.tensor(anomaly_score, device=device)

    precision, recall, thresholds = precision_recall_curve(anomaly_score_tensor, image_targets_tensor, task='binary')
    f1_scores = 2 * precision * recall / (precision + recall + 1e-8)

    best_id = torch.argmax(f1_scores)
    best_f1 = f1_scores[best_id]
    best_threshold = thresholds[best_id]

    return best_f1, best_threshold


def create_heatmaps(input_image, anomaly_map, ground_truth, save_path, file_path):
    if not os.path.exists(os.path.dirname(save_path)):
        os.mkdir(os.path.dirname(save_path))

    if len(input_image.shape) == 3:
        input_img = input_image.permute(1, 2, 0).numpy()
        if input_img.shape[2] == 3:  # RGB
            input_img = (input_img * 255).astype(np.uint8)
        else:  
            input_img = (input_img.squeeze() * 255).astype(np.uint8)
    else:  # (H,W)
        input_img = (input_image.numpy() * 255).astype(np.uint8)

    h, w = input_img.shape[:2] if len(input_img.shape) == 3 else input_image.shape
    ground_truth = (ground_truth.reshape(h, w) * 255).astype(np.uint8)
    anomaly_map = anomaly_map.reshape(h, w)

    if len(input_img.shape) == 2:
        input_img = np.stack([input_img]*3, axis=-1)
    
    min_score = np.min(anomaly_map)
    max_score = np.max(anomaly_map)
    norm_heatmap = (anomaly_map - min_score) / (max_score - min_score + 1e-10)

    heatmap_resized = cv.resize(norm_heatmap, (w, h))
    heatmap_8bit = (heatmap_resized * 255).astype(np.uint8)
    heatmap_bgr = cv.cvtColor(heatmap_8bit, cv.COLOR_GRAY2BGR)
    heatmap_color = cv.applyColorMap(heatmap_bgr, cv.COLORMAP_JET)

    ground_truth_bgr = cv.cvtColor(ground_truth, cv.COLOR_GRAY2BGR)

    combined = np.hstack([input_img, ground_truth_bgr, heatmap_color])
    combined_resized = cv.resize(combined, (256 * 3, 256))  

    cv.imwrite(save_path, combined_resized)








