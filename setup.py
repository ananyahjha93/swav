import os
from setuptools import setup, find_packages


PATH_ROOT = os.path.dirname(__file__)

def load_requirements(path_dir=PATH_ROOT, comment_char='#'):
    with open(os.path.join(path_dir, 'requirements.txt'), 'r') as file:
        lines = [ln.strip() for ln in file.readlines()]
    reqs = [ln[:ln.index(comment_char)] if comment_char in ln else ln for ln in lines]
    reqs = [ln for ln in reqs if ln]
    return reqs

setup(
   name='swav',
   packages=['swav'],  #same as name
   version='1.0',
   url='https://github.com/ananyahjha93/swav',
   maintainer='Ananya Harsh Jha',
   maintainer_email='ananya@pytorchlightning.ai',
   install_requires=[
       'numpy', 'opencv-python', 'pandas', 'pytorch-lightning',
       'scikit-learn', 'scipy', 'tensorboard', 'torch',
       'torchvision', 'tqdm'
    ], #external packages as dependencies
   scripts=[
            'src/logger',
            'src/multicropdataset',
            'src/resnet50',
            'src/stl10_datamodule',
            'src/swav_transforms',
            'src/utils',
            'eval_linear',
            'eval_semisup'
           ]
)
