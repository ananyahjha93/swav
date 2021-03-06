import os
import torch
import numpy as np
from PIL import Image
from pytorch_lightning import LightningDataModule
from torch.utils.data import DataLoader, random_split, ConcatDataset
from torchvision import transforms as transform_lib
from torchvision.datasets import STL10

from pl_bolts.transforms.dataset_normalizations import stl10_normalization


class UnsupervisedSTL10(STL10):
    def __getitem__(self, index):
        if self.labels is not None:
            img, target = self.data[index], int(self.labels[index])
        else:
            img, target = self.data[index], None

        # doing this so that it is consistent with all other datasets
        # to return a PIL Image
        img = Image.fromarray(np.transpose(img, (1, 2, 0)))

        if self.transform is not None:
            img = self.transform(img)

        if self.target_transform is not None:
            target = self.target_transform(target)

        return img


class STL10DataModule(LightningDataModule):  # pragma: no cover

    name = 'stl10'

    def __init__(
            self,
            data_dir: str = None,
            train_dist_sampler: bool = False,
            val_dist_sampler: bool = False,
            test_dist_sampler: bool = False,
            unlabeled_val_split: int = 5000,
            train_val_split: int = 500,
            num_workers: int = 16,
            batch_size: int = 32,
            seed: int = 42,
            *args,
            **kwargs,
    ):
        """
        .. figure:: https://samyzaf.com/ML/cifar10/cifar1.jpg
            :width: 400
            :alt: STL-10

        Specs:
            - 10 classes (1 per type)
            - Each image is (3 x 96 x 96)

        Standard STL-10, train, val, test splits and transforms.
        STL-10 has support for doing validation splits on the labeled or unlabeled splits

        Added dist_sampler bool option to use datamodule without lightning

        Transforms::

            mnist_transforms = transform_lib.Compose([
                transform_lib.ToTensor(),
                transforms.Normalize(
                    mean=(0.43, 0.42, 0.39),
                    std=(0.27, 0.26, 0.27)
                )
            ])

        Example::

            from pl_bolts.datamodules import STL10DataModule

            dm = STL10DataModule(PATH)
            model = LitModel()

            Trainer().fit(model, dm)

        Args:
            data_dir: where to save/load the data
            train_dist_sampler: boolean to enable distributed sampler in train loader
            val_dist_sampler: boolean to enable distributed sampler in val loader
            test_dist_sampler: boolean to enable distributed sampler in test loader
            unlabeled_val_split: how many images from the unlabeled training split to use for validation
            train_val_split: how many images from the labeled training split to use for validation
            num_workers: how many workers to use for loading data
            batch_size: the batch size
        """
        super().__init__(*args, **kwargs)

        self.dims = (3, 96, 96)
        self.data_dir = data_dir if data_dir is not None else os.getcwd()

        self.train_dist_sampler = train_dist_sampler
        self.val_dist_sampler = val_dist_sampler
        self.test_dist_sampler = test_dist_sampler

        self.unlabeled_val_split = unlabeled_val_split
        self.train_val_split = train_val_split
        self.num_unlabeled_samples = 100000 - unlabeled_val_split
        self.num_labeled_samples = 5000 - train_val_split

        self.num_workers = num_workers
        self.batch_size = batch_size

        self.seed = seed

    @property
    def num_classes(self):
        return 10

    def prepare_data(self):
        """
        Downloads the unlabeled, train and test split
        """
        UnsupervisedSTL10(self.data_dir, split='unlabeled', download=True, transform=transform_lib.ToTensor())
        UnsupervisedSTL10(self.data_dir, split='train', download=True, transform=transform_lib.ToTensor())
        UnsupervisedSTL10(self.data_dir, split='test', download=True, transform=transform_lib.ToTensor())

    def train_dataloader(self):
        """
        Loads the 'unlabeled' split minus a portion set aside for validation via `unlabeled_val_split`.
        """
        transforms = self.default_transforms() if self.train_transforms is None else self.train_transforms

        dataset = UnsupervisedSTL10(self.data_dir, split='unlabeled', download=False, transform=transforms)
        train_length = len(dataset)
        dataset_train, _ = random_split(
            dataset,
            [train_length - self.unlabeled_val_split, self.unlabeled_val_split],
            generator=torch.Generator().manual_seed(self.seed)
        )

        sampler = None
        if self.train_dist_sampler:
            sampler = torch.utils.data.distributed.DistributedSampler(dataset_train)

        loader = DataLoader(
            dataset_train,
            batch_size=self.batch_size,
            sampler=sampler,
            shuffle=True if sampler is None else False,
            num_workers=self.num_workers,
            drop_last=True,
            pin_memory=True
        )

        return loader

    def train_dataloader_mixed(self):
        """
        Loads a portion of the 'unlabeled' training data and 'train' (labeled) data.
        both portions have a subset removed for validation via `unlabeled_val_split` and `train_val_split`

        Args:

            batch_size: the batch size
            transforms: a sequence of transforms
        """
        transforms = self.default_transforms() if self.train_transforms is None else self.train_transforms

        unlabeled_dataset = UnsupervisedSTL10(
            self.data_dir, split='unlabeled', download=False, transform=transforms
        )
        unlabeled_length = len(unlabeled_dataset)
        unlabeled_dataset, _ = random_split(
            unlabeled_dataset,
            [unlabeled_length - self.unlabeled_val_split, self.unlabeled_val_split],
            generator=torch.Generator().manual_seed(self.seed)
        )

        labeled_dataset = UnsupervisedSTL10(self.data_dir, split='train', download=False, transform=transforms)
        labeled_length = len(labeled_dataset)
        labeled_dataset, _ = random_split(
            labeled_dataset,
            [labeled_length - self.train_val_split, self.train_val_split],
            generator=torch.Generator().manual_seed(self.seed)
        )

        dataset = ConcatDataset([unlabeled_dataset, labeled_dataset])

        sampler = None
        if self.train_dist_sampler:
            sampler = torch.utils.data.distributed.DistributedSampler(dataset)

        loader = DataLoader(
            dataset,
            batch_size=self.batch_size,
            sampler=sampler,
            shuffle=True if sampler is None else False,
            num_workers=self.num_workers,
            drop_last=True,
            pin_memory=True
        )

        return loader

    def val_dataloader(self):
        """
        Loads a portion of the 'unlabeled' training data set aside for validation
        The val dataset = (unlabeled - train_val_split)

        Args:

            batch_size: the batch size
            transforms: a sequence of transforms
        """
        transforms = self.default_transforms() if self.val_transforms is None else self.val_transforms

        dataset = UnsupervisedSTL10(self.data_dir, split='unlabeled', download=False, transform=transforms)
        train_length = len(dataset)

        _, dataset_val = random_split(
            dataset,
            [train_length - self.unlabeled_val_split, self.unlabeled_val_split],
            generator=torch.Generator().manual_seed(self.seed)
        )

        sampler = None
        if self.val_dist_sampler:
            sampler = torch.utils.data.distributed.DistributedSampler(dataset_val)

        loader = DataLoader(
            dataset_val,
            batch_size=self.batch_size,
            sampler=sampler,
            shuffle=False,
            num_workers=self.num_workers,
            pin_memory=True
        )

        return loader

    def val_dataloader_mixed(self):
        """
        Loads a portion of the 'unlabeled' training data set aside for validation along with
        the portion of the 'train' dataset to be used for validation

        unlabeled_val = (unlabeled - train_val_split)

        labeled_val = (train- train_val_split)

        full_val = unlabeled_val + labeled_val

        Args:

            batch_size: the batch size
            transforms: a sequence of transforms
        """
        transforms = self.default_transforms() if self.val_transforms is None else self.val_transforms

        unlabeled_dataset = UnsupervisedSTL10(
            self.data_dir, split='unlabeled', download=False, transform=transforms
        )
        unlabeled_length = len(unlabeled_dataset)
        _, unlabeled_dataset = random_split(
            unlabeled_dataset,
            [unlabeled_length - self.unlabeled_val_split, self.unlabeled_val_split],
            generator=torch.Generator().manual_seed(self.seed)
        )

        labeled_dataset = UnsupervisedSTL10(self.data_dir, split='train', download=False, transform=transforms)
        labeled_length = len(labeled_dataset)
        _, labeled_dataset = random_split(
            labeled_dataset,
            [labeled_length - self.train_val_split, self.train_val_split],
            generator=torch.Generator().manual_seed(self.seed)
        )

        dataset = ConcatDataset([unlabeled_dataset, labeled_dataset])

        sampler = None
        if self.val_dist_sampler:
            sampler = torch.utils.data.distributed.DistributedSampler(dataset)

        loader = DataLoader(
            dataset,
            batch_size=self.batch_size,
            shuffle=False,
            sampler=sampler,
            num_workers=self.num_workers,
            drop_last=True,
            pin_memory=True
        )

        return loader

    def test_dataloader(self):
        """
        Loads the test split of STL10

        Args:
            batch_size: the batch size
            transforms: the transforms
        """
        transforms = self.default_transforms() if self.test_transforms is None else self.test_transforms
        dataset = UnsupervisedSTL10(self.data_dir, split='test', download=False, transform=transforms)

        sampler = None
        if self.test_dist_sampler:
            sampler = torch.utils.data.distributed.DistributedSampler(dataset)

        loader = DataLoader(
            dataset,
            batch_size=self.batch_size,
            sampler=sampler,
            shuffle=False,
            num_workers=self.num_workers,
            drop_last=True,
            pin_memory=True
        )

        return loader

    def train_dataloader_labeled(self):
        transforms = self.default_transforms() if self.val_transforms is None else self.val_transforms

        dataset = UnsupervisedSTL10(self.data_dir, split='train', download=False, transform=transforms)
        train_length = len(dataset)
        dataset_train, _ = random_split(
            dataset,
            [train_length - self.num_labeled_samples, self.num_labeled_samples],
            generator=torch.Generator().manual_seed(self.seed)
        )

        sampler = None
        if self.train_dist_sampler:
            sampler = torch.utils.data.distributed.DistributedSampler(dataset_train)

        loader = DataLoader(
            dataset_train,
            batch_size=self.batch_size,
            sampler=sampler,
            shuffle=True if sampler is None else False,
            num_workers=self.num_workers,
            pin_memory=True
        )

        return loader

    def val_dataloader_labeled(self):
        transforms = self.default_transforms() if self.val_transforms is None else self.val_transforms
        dataset = UnsupervisedSTL10(
            self.data_dir, split='train', download=False, transform=transforms
        )

        labeled_length = len(dataset)
        _, labeled_val = random_split(
            dataset,
            [labeled_length - self.num_labeled_samples, self.num_labeled_samples],
            generator=torch.Generator().manual_seed(self.seed)
        )

        sampler = None
        if self.val_dist_sampler:
            sampler = torch.utils.data.distributed.DistributedSampler(labeled_val)

        loader = DataLoader(
            labeled_val,
            batch_size=self.batch_size,
            sampler=sampler,
            shuffle=False,
            num_workers=self.num_workers,
            drop_last=True,
            pin_memory=True
        )

        return loader

    def default_transforms(self):
        data_transforms = transform_lib.Compose([
            transform_lib.ToTensor(),
            stl10_normalization()
        ])
        return data_transforms
