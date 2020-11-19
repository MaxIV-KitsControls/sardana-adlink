#!/usr/bin/env python


from setuptools import setup, find_packages

# The version is updated automatically with bumpversion
# Do not update manually
__version = '2.0.0'


def main():
    """Main method collecting all the parameters to setup."""
    name = "sardana-adlink"

    version = __version

    description = "AdlinkAICoTi Sardana Controller"

    author = "ALBA"

    author_email = "controls@cells.es"

    license = "GPLv3"

    url = "https://github.com/ALBA-Synchrotron/sardana-adlink"

    packages = find_packages()

    # Add your dependencies in the following line.
    install_requires = ['sardana']

    setup(
        name=name,
        version=version,
        description=description,
        author=author,
        author_email=author_email,
        license=license,
        url=url,
        packages=packages,
        install_requires=install_requires
    )


if __name__ == "__main__":
    main()
