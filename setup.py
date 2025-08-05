from setuptools import setup

setup(
    name="airthings_cloud",
    packages=["airthings"],
    install_requires=["aiohttp>=3.0.6", "async_timeout>=3.0.0"],
    version="0.2.0",
    description="A python3 library to communicate with Airthings devices",
    python_requires=">=3.13.0",
    author="Daniel Hjelseth Høyer",
    author_email="github@dahoiv.net",
    url="https://github.com/Danielhiversen/pyAirthings",
    license="MIT",
    classifiers=[
        "Intended Audience :: Developers",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
