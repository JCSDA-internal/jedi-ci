import unittest
import subprocess
import os
from pathlib import Path


class TestCodeStyle(unittest.TestCase):
    def test_pycodestyle_check(self):
        """Test that the code follows PEP 8 style guidelines using pycodestyle"""
        # Get the project root directory
        project_root = Path(__file__).parent.parent
        
        # Define directories to check
        directories_to_check = [
            os.path.join(project_root, "ci_action"),
            os.path.join(project_root, "operation_tools"),
        ]
        
        # Filter out directories that don't exist
        existing_dirs = [d for d in directories_to_check if os.path.exists(d)]
        
        if not existing_dirs:
            self.fail("No source directories found to check")
        
        # Run pycodestyle on each directory
        for directory in existing_dirs:
            with self.subTest(directory=directory):
                result = subprocess.run(
                    ["pycodestyle", "--max-line-length=100", directory],
                    capture_output=True,
                    text=True
                )
                
                # If there are style violations, include them in the failure message
                if result.returncode != 0:
                    self.fail(
                        f"pycodestyle found style violations in {directory}:\n"
                        f"{result.stdout}\n{result.stderr}"
                    )


if __name__ == "__main__":
    unittest.main() 