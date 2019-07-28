# -*- coding: utf-8 -*-
# File: base.py

import os
import inspect
import pprint
from collections import namedtuple
import weakref

from ...utils.argtools import log_once
from ...utils.utils import get_rng
from ..image import check_dtype

# Cannot import here if we want to keep backward compatibility.
# Because this causes circular dependency
# from .transform import TransformList, PhotometricTransform, TransformFactory

__all__ = ['Augmentor', 'ImageAugmentor', 'AugmentorList', 'PhotometricAugmentor']


def _reset_augmentor_after_fork(aug_ref):
    aug = aug_ref()
    if aug:
        aug.reset_state()


ImagePlaceholder = namedtuple("ImagePlaceholder", ["shape"])


class ImageAugmentor(object):
    """ Base class for an augmentor

    ImageAugmentor should take images of type uint8 in range [0, 255], or
    floating point images in range [0, 1] or [0, 255].
    """

    def __init__(self):
        self.reset_state()

        # only available on Unix after Python 3.7
        if hasattr(os, 'register_at_fork'):
            os.register_at_fork(
                after_in_child=lambda: _reset_augmentor_after_fork(weakref.ref(self)))

    def _init(self, params=None):
        if params:
            for k, v in params.items():
                if k != 'self' and not k.startswith('_'):
                    setattr(self, k, v)

    def reset_state(self):
        """
        Reset rng and other state of the augmentor.

        Similar to :meth:`DataFlow.reset_state`, the caller of Augmentor
        is responsible for calling this method (once or more times) in the **process that uses the augmentor**
        before using it.

        If you use a built-in augmentation dataflow (:class:`AugmentImageComponent`, etc),
        this method will be called in the dataflow's own `reset_state` method.

        If you use Python≥3.7 on Unix, this method will be automatically called after fork,
        and you do not need to bother calling it.
        """
        self.rng = get_rng(self)

    def _rand_range(self, low=1.0, high=None, size=None):
        """
        Uniform float random number between low and high.
        """
        if high is None:
            low, high = 0, low
        if size is None:
            size = []
        return self.rng.uniform(low, high, size)

    def __repr__(self):
        """
        Produce something like:
        "imgaug.MyAugmentor(field1={self.field1}, field2={self.field2})"
        """
        try:
            argspec = inspect.getargspec(self.__init__)
            assert argspec.varargs is None, "The default __repr__ doesn't work for varargs!"
            assert argspec.keywords is None, "The default __repr__ doesn't work for kwargs!"
            fields = argspec.args[1:]
            index_field_has_default = len(fields) - (0 if argspec.defaults is None else len(argspec.defaults))

            classname = type(self).__name__
            argstr = []
            for idx, f in enumerate(fields):
                assert hasattr(self, f), \
                    "Attribute {} in {} not found! Default __repr__ only works if " \
                    "attributes match the constructor.".format(f, classname)
                attr = getattr(self, f)
                if idx >= index_field_has_default:
                    if attr is argspec.defaults[idx - index_field_has_default]:
                        continue
                argstr.append("{}={}".format(f, pprint.pformat(attr)))
            return "imgaug.{}({})".format(classname, ', '.join(argstr))
        except AssertionError as e:
            log_once(e.args[0], 'warn')
            return super(Augmentor, self).__repr__()

    def get_transform(self, img):
        """
        Instantiate a :class:`Transform` object from the given image.

        Args:
            img (ndarray):

        Returns:
            Transform
        """
        # This should be an abstract method
        # But we provide an implementation that uses the old interface,
        # for backward compatibility

        def legacy_augment_coords(self, coords, p):
            try:
                return self._augment_coords(coords, p)
            except AttributeError:
                pass
            try:
                return self.augment_coords(coords, p)
            except AttributeError:
                pass
            return coords  # this is the old default

        p = None  # the default return value for this method
        try:
            p = self._get_augment_params(img)
        except AttributeError:
            pass
        try:
            p = self.get_augment_params(img)
        except AttributeError:
            pass

        from .transform import Transform
        if isinstance(p, Transform):  # some old augs return Transform already
            return p

        from .transform import TransformFactory
        return TransformFactory(name="LegacyConversion -- " + str(self),
                                apply_image=lambda img: self._augment(img, p),
                                apply_coords=lambda coords: legacy_augment_coords(self, coords, p))

    def augment(self, img):
        t = self.get_transform(img)
        return t.apply_image(img)

    def augment_return_tfm(self, img):
        t = self.get_transform(img)
        return t.apply_image(img), t

    __str__ = __repr__

    # ###########################
    # Legacy interfaces:
    # ###########################
    def augment_return_params(self, d):
        t = self.get_transform(d)
        return t.apply_image(d), t

    def augment_with_params(self, d, param):
        return param.apply_image(d)

    def augment_coords(self, coords, param):
        return param.apply_coords(coords)


class AugmentorList(ImageAugmentor):
    """
    Augment an image by a list of augmentors
    """

    def __init__(self, augmentors):
        """
        Args:
            augmentors (list): list of :class:`ImageAugmentor` instance to be applied.
        """
        assert isinstance(augmentors, (list, tuple)), augmentors
        self.augmentors = augmentors
        super(AugmentorList, self).__init__()

    def reset_state(self):
        """ Will reset state of each augmentor """
        super(AugmentorList, self).reset_state()
        for a in self.augmentors:
            a.reset_state()

    def get_transform(self, img):
        # the next augmentor requires the previous one to finish
        raise RuntimeError("Cannot simply get transform of a AugmentorList without running the augmentation!")

    def augment_return_tfm(self, img):
        check_dtype(img)
        assert img.ndim in [2, 3], img.ndim

        tfms = []
        for a in self.augmentors:
            t = a.get_transform(img)
            img = t.apply_image(img)
            tfms.append(t)
        from .transform import TransformList
        return img, TransformList(tfms)

    def augment(self, d):
        return self.augment_return_tfm(d)[0]

    # ###########################
    # Legacy interfaces:
    # ###########################

    def augment_return_params(self, d):
        return self.augment_return_tfm(d)


Augmentor = ImageAugmentor
"""
Legacy name. Augmentor and ImageAugmentor are now the same thing.
"""


class PhotometricAugmentor(ImageAugmentor):
    def get_transform(self, img):
        p = self._get_params(img)
        from .transform import PhotometricTransform
        return PhotometricTransform(func=lambda img: self._impl(img, p),
                                    name=str(self))

    def _get_params(self, _):
        return None
