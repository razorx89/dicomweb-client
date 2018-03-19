#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import sys

import setuptools


def get_version():
    src_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'src')
    sys.path = [src_path] + sys.path
    import dicomweb_client
    return dicomweb_client.__version__


setuptools.setup(
    name='dicomweb-client',
    version=get_version(),
    description='Client for DICOMweb RESTful services.',
    author='Markus D. Herrmann',
    maintainer='Markus D. Herrmann',
    website='https://github.com/clindatsci/dicomweb-python-client',
    license='MIT',
    platforms=['Linux', 'MacOS', 'Windows'],
    classifiers=[
        'Environment :: Web Environment',
        'License :: OSI Approved :: MIT License',
        'Operating System :: MacOS',
        'Operating System :: Microsoft :: Windows',
        'Operating System :: POSIX :: Linux',
        'Intended Audience :: Science/Research',
        'Topic :: Internet :: WWW/HTTP',
        'Topic :: Multimedia :: Graphics',
        'Topic :: Scientific/Engineering :: Information Analysis',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Development Status :: 4 - Beta',
    ],
    entry_points={
        'console_scripts': ['dicomweb_client = dicomweb_client.cli:main'],
    },
    include_package_data=True,
    packages=setuptools.find_packages('src'),
    package_dir={'': 'src'},
    setup_requires=[
        'pytest-runner>=3.0',
    ],
    tests_require=[
        'pytest>=3.3',
        'pytest-localserver>=0.4',
        'pytest-flake8>=0.9',
    ],
    install_requires=[
        'numpy>=1.13',
        'pillow>=5.0',
        'pydicom>=1.0',
        'requests>=2.18',
    ]
)