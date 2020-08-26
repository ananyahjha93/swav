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
   packages=find_packages(),
   install_requires=load_requirements(PATH_ROOT)
)