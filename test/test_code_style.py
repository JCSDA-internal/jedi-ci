import unittest
import subprocess
import os
from pathlib import Path


class TestFlake8(unittest.TestCase):
    def test_flake8_check(self):
        """Test that the code follows PEP 8 style guidelines using flake8"""
        project_root = Path(__file__).parent.parent
        ci_action_dir = os.path.join(project_root, "ci_action")

        flake8_command = [
            'flake8',
            '--toml-config',
            os.path.join(project_root, 'pyproject.toml'),
            ci_action_dir,
        ]
        result = subprocess.run(flake8_command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"Flake8 found style violations in {ci_action_dir}:\n{result.stdout}")



if __name__ == "__main__":
    unittest.main() 