import json
import numpy as np
from PatchDataModule_wMask_crossval import *
from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint, ModelSummary
from pytorch_lightning.loggers import TensorBoardLogger
import torch
from model_wMask import *
from monai.losses.dice import DiceLoss
torch.cuda.empty_cache()

prepared_data_path = ['path_to_real_data/', 'path_to_synthetic_data/']
test_path = 'path_to_test_data/'
results_path = 'path_to_experiment/results/'
experiment_path = 'path_to_experiment/experiment/'
MAX_EPOCHS = 150
PATIENCE = 10
NUM_FOLDS = 30
subfolder = ['lightning_logs','Model_checkpoints']
for subf in subfolder:
    if not os.path.isdir(experiment_path + subf):
        os.mkdir(experiment_path + subf)

features = {}
def get_features(name):
    def hook(model, input, output):
        features[name] = output.detach()
    return hook


StrokeDM = PatchDataModule_wMask_crossval(prepared_data_path=prepared_data_path, test_path=test_path,
                                patch_size=(64,64,64), patch_step=(16,16,16), do_skull_stripping=False, 
                                batch_size=8, validation_fraction=0.2, num_folds = NUM_FOLDS, num_workers=14, 
                                do_data_augmentation=False, patches_per_image=5)

# loss function

def make_one_hot(labels, classes):
    one_hot = torch.cuda.FloatTensor(labels.size()[0], classes, labels.size()[2], labels.size()[3],
                                     labels.size()[4]).zero_()
    target = one_hot.scatter_(1, labels.data, 1)
    return target


class SimLoss(nn.Module):
    '''
    Intensity mean squared error loss
    '''
    def __init__(self):
        super(SimLoss, self).__init__()

    def forward(self, output, target):
        loss = torch.mean((target - output) ** 2)

        return loss

class SmoothLoss(nn.Module):
    '''
    Smooth DVF regularization to avoid the unrealistic image generation
    Usually it is a spatial gradient of the DVF
    '''
    """
    N-D gradient loss.
    """

    def __init__(self):
        super(SmoothLoss, self).__init__()

    def forward(self, output):
        dy = torch.abs(output[:, :, 1:, :, :] - output[:, :, :-1, :, :])
        dx = torch.abs(output[:, :, :, 1:, :] - output[:, :, :, :-1, :])
        dz = torch.abs(output[:, :, :, :, 1:] - output[:, :, :, :, :-1])
        # Return tensors with same size as original image by concatenating zeros.
        dy = torch.cat((dy,torch.zeros(output.size(dim=0),output.size(dim=1),1,output.size(dim=3),output.size(dim=4)).to(device='cuda')),dim=2)
        dx = torch.cat((dx,torch.zeros(output.size(dim=0),output.size(dim=1),output.size(dim=2),1,output.size(dim=4)).to(device='cuda')),dim=3)
        dz = torch.cat((dz,torch.zeros(output.size(dim=0),output.size(dim=1),output.size(dim=2),output.size(dim=3),1).to(device='cuda')),dim=4)


        dy = dy * dy
        dx = dx * dx
        dz = dz * dz

        d = torch.mean(dx) + torch.mean(dy) + torch.mean(dz)
        grad = d / 3.0

        return torch.tensor(grad)

seg_loss = DiceLoss()
my_loss = torch.nn.L1Loss()
smooth_loss = SmoothLoss()
my_dvfLoss = lambda output: smooth_loss(output)
my_simloss = lambda output, target: my_loss(output,target)
my_segloss = lambda output, target: seg_loss(input=output, target=target)



image_measures = {}

for fold in range(NUM_FOLDS):
    print(f"Training on fold number {fold}...")
    StrokeDM.fold_index = fold
    if fold > 0:
        StrokeDM.set_fold()




    logger = TensorBoardLogger(experiment_path + 'lightning_logs/'+ 'fold_' + str(fold) + '/', log_graph=True )

    early_stopping_callback = EarlyStopping(monitor='val_loss',
                                                patience=PATIENCE,
                                                min_delta=1e-6,
                                                verbose=True,
                                                mode='min')

    checkpoint_callback = ModelCheckpoint(dirpath=os.path.join(experiment_path,'Model_checkpoints', f'fold{fold}'),
                                        filename='trueta' + '-{epoch:02d}',
                                        monitor='val_loss',
                                        mode='min',
                                        verbose=False)

    pl.seed_everything(0, workers=True)


    model = FullModel(2,3,my_dvfLoss,my_simloss, my_segloss)

    trainer = pl.Trainer(max_epochs=MAX_EPOCHS,
                        strategy="dp",
                        accelerator='gpu', devices=[0,1], 
                        callbacks=[early_stopping_callback, checkpoint_callback,ModelSummary(max_depth=-1)],
                        deterministic=False,
                        fast_dev_run=False, 
                        enable_model_summary=False,
                        logger=logger)

    trainer.fit(model, StrokeDM)
    StrokeDM.setup(stage='test')


    test_cases = StrokeDM.get_test_cases()

    image_measures.update(
        model.infer_test_images(test_cases=test_cases, 
                                Stroke_DM=StrokeDM, 
                                filepath_out=results_path))



with open(os.path.join(results_path, 'image_measures.json'), 'w') as f:
        json.dump(image_measures, f, indent=2)
