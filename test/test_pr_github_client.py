import unittest
from ci_action.library.github_client import get_fullname_from_github_uri, get_repo_tuple_from_github_uri

class TestPrResolve(unittest.TestCase):
    def testget_fullname_from_github_uri_with_git_suffix(self):
        """Test that get_fullname_from_github_uri works with URLs ending in .git"""
        url = "https://github.com/jcsda-internal/oops.git"
        expected = "jcsda-internal/oops"
        self.assertEqual(get_fullname_from_github_uri(url), expected)
    
    def testget_fullname_from_github_uri_without_git_suffix(self):
        """Test that get_fullname_from_github_uri works with URLs without .git ending"""
        url = "https://github.com/jcsda-internal/oops"
        expected = "jcsda-internal/oops"
        self.assertEqual(get_fullname_from_github_uri(url), expected)
    
    def testget_repo_tuple_from_github_uri_with_git_suffix(self):
        """Test that get_repo_tuple_from_github_uri correctly extracts repo and org with .git suffix"""
        url = "https://github.com/jcsda-internal/oops.git"
        expected_repo, expected_org = "oops", "jcsda-internal"
        repo, org = get_repo_tuple_from_github_uri(url)
        self.assertEqual(repo, expected_repo)
        self.assertEqual(org, expected_org)
    
    def testget_repo_tuple_from_github_uri_without_git_suffix(self):
        """Test that get_repo_tuple_from_github_uri correctly extracts repo and org without .git suffix"""
        url = "https://github.com/jcsda-internal/oops"
        expected_repo, expected_org = "oops", "jcsda-internal"
        repo, org = get_repo_tuple_from_github_uri(url)
        self.assertEqual(repo, expected_repo)
        self.assertEqual(org, expected_org)

if __name__ == "__main__":
    unittest.main() 