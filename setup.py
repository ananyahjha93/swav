from setuptools import setup

setup(
   name='swav',
   packages=['swav'],  #same as name
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
