from setuptools import setup

setup(
   name='swav',
   packages=['swav'],  #same as name
   scripts=[
            'src/logger',
            'src/multicropdataset',
            'src/resnet50',
            'src/stl10_datamodule',
            'src/swav_transforms',
            'src/utils'
           ]
)
