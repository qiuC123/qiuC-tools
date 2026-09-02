from setuptools import find_namespace_packages, setup


setup(
    name="cli-anything-gpt-sovits",
    version="0.1.0",
    description="Agent-friendly local inference CLI for GPT-SoVITS",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    include_package_data=True,
    package_data={"cli_anything.gpt_sovits": ["skills/*.md"]},
    install_requires=["click>=8.1", "prompt-toolkit>=3.0", "psutil>=5.9", "PyYAML>=6.0"],
    extras_require={"test": ["pytest>=8.0"]},
    entry_points={
        "console_scripts": [
            "cli-anything-gpt-sovits=cli_anything.gpt_sovits.gpt_sovits_cli:main",
        ]
    },
    python_requires=">=3.10",
)
