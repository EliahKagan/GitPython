#!/usr/bin/env python

from pathlib import Path

from setuptools import setup, find_packages


def _read_content(path: str) -> str:
    return (Path(__file__).parent / path).read_text(encoding="utf-8")


version = _read_content("VERSION").strip()
requirements = _read_content("requirements.txt").splitlines()
test_requirements = _read_content("test-requirements.txt").splitlines()
doc_requirements = _read_content("doc/requirements.txt").splitlines()
long_description = _read_content("README.md")


setup(
    name="GitPython",
    version=version,
    description="GitPython is a Python library used to interact with Git repositories",
    author="Sebastian Thiel, Michael Trier",
    author_email="byronimo@gmail.com, mtrier@gmail.com",
    license="BSD-3-Clause",
    url="https://github.com/gitpython-developers/GitPython",
    packages=find_packages(exclude=["test", "test.*"]),
    include_package_data=True,
    package_dir={"git": "git"},
    python_requires=">=3.7",
    install_requires=requirements,
    extras_require={
        "test": test_requirements,
        "doc": doc_requirements,
    },
    zip_safe=False,
    long_description=long_description,
    long_description_content_type="text/markdown",
    classifiers=[
        # Picked from
        #   http://pypi.python.org/pypi?:action=list_classifiers
        # "Development Status :: 1 - Planning",
        # "Development Status :: 2 - Pre-Alpha",
        # "Development Status :: 3 - Alpha",
        # "Development Status :: 4 - Beta",
        "Development Status :: 5 - Production/Stable",
        # "Development Status :: 6 - Mature",
        # "Development Status :: 7 - Inactive",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: BSD License",
        "Operating System :: OS Independent",
        "Operating System :: POSIX",
        "Operating System :: Microsoft :: Windows",
        "Operating System :: MacOS :: MacOS X",
        "Typing :: Typed",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
