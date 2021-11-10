from setuptools import setup

install_requires = [
    "aiohttp",
    "async_timeout"
]


setup(
    name="airthings_cloud",
    packages=["airthings"],
    install_requires=install_requires,
    version="0.0.1",
    description="A python3 library to communicate with Airthings devices",
    python_requires=">=3.7.0",
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
