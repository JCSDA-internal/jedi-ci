import unittest
from io import StringIO
from ci_action.library.cmake_rewrite import BundleLine, CMakeFile

# Example bundle lines for testing
SIMPLE_GIT_BUNDLE_LINE = 'ecbuild_bundle( PROJECT myproject GIT "https://github.com/myorg/MyRepo.git" BRANCH mybranch UPDATE RECURSIVE )'
SIMPLE_TAG_LINE = 'ecbuild_bundle( PROJECT myproject GIT "https://github.com/myorg/MyRepo.git" TAG v1.2.3 )'
TYPICAL_GIT_BUNDLE_LINE = 'ecbuild_bundle( PROJECT oops     GIT "https://github.com/jcsda-internal/oops.git"        BRANCH develop UPDATE )'

class TestBundleLine(unittest.TestCase):

    def test_oops_rewrite_original(self):
        in_line = TYPICAL_GIT_BUNDLE_LINE
        # Rewriting a line removes the extra formatting whitespace.
        want_line = 'ecbuild_bundle( PROJECT oops GIT "https://github.com/jcsda-internal/oops.git" BRANCH develop UPDATE )'
        bl = BundleLine(in_line)
        got_line = bl.rewrite_original()
        self.assertEqual(got_line, want_line)

    def test_oops_rewrite_as_git_commit_hash(self):
        # Note that "UPDATE" is an attribute only used with branches, for a tag it will be
        # removed when rewriting to a tag line.
        in_line = 'ecbuild_bundle( PROJECT oops     GIT "https://github.com/jcsda-internal/oops.git"        BRANCH develop UPDATE )'
        want_line = 'ecbuild_bundle( PROJECT oops GIT "https://github.com/jcsda-internal/oops.git" TAG 174cf5175ba0e7d256d243d301e0510dd94e23de )'
        bl = BundleLine(in_line)
        got_line = bl.rewrite(tag='174cf5175ba0e7d256d243d301e0510dd94e23de')
        self.assertEqual(got_line, want_line)

    def test_bundle_line_original_line(self):
        bl = BundleLine(SIMPLE_GIT_BUNDLE_LINE)
        self.assertEqual(bl.original_line(), SIMPLE_GIT_BUNDLE_LINE)

    def test_parse_git_bundle_line(self):
        bl = BundleLine(SIMPLE_GIT_BUNDLE_LINE)
        self.assertEqual(bl.project.value, 'myproject')
        self.assertEqual(bl.source_reference.value, 'https://github.com/myorg/MyRepo.git')
        self.assertEqual(bl.source_reference_type, 'git')
        self.assertEqual(bl.version_ref_type, 'branch')

    def test_parse_tag_bundle_line(self):
        bl = BundleLine(SIMPLE_TAG_LINE)
        self.assertEqual(bl.project.value, 'myproject')
        self.assertEqual(bl.source_reference.value, 'https://github.com/myorg/MyRepo.git')
        self.assertEqual(bl.source_reference_type, 'git')
        self.assertEqual(bl.version_ref_type, 'tag')
        # Org/repo key is derived from the git URI and is lowercased.
        self.assertEqual(bl.github_org_repo_key, 'myorg/myrepo')

    def test_parse_source_bundle_line(self):
        source_line = 'ecbuild_bundle( PROJECT myproject SOURCE /path/to/source )'
        bl = BundleLine(source_line)
        self.assertEqual(bl.project.value, 'myproject')
        self.assertEqual(bl.source_reference.value, '/path/to/source')
        self.assertEqual(bl.source_reference_type, 'source')

    def test_regenerate_original_line(self):
        bl = BundleLine(SIMPLE_GIT_BUNDLE_LINE)
        regen = bl.rewrite_original()
        self.assertIn('ecbuild_bundle(', regen)
        self.assertIn('PROJECT myproject', regen)
        self.assertIn('GIT "https://github.com/myorg/MyRepo.git"', regen)
        self.assertTrue('BRANCH mybranch' in regen or 'TAG' not in regen)

    def test_rewrite_with_new_branch(self):
        bl = BundleLine(SIMPLE_GIT_BUNDLE_LINE)
        new_line = bl.rewrite(branch='newbranch')
        self.assertIn('BRANCH newbranch', new_line)
        self.assertNotIn('TAG', new_line)

    def test_rewrite_with_new_tag(self):
        bl = BundleLine(SIMPLE_GIT_BUNDLE_LINE)
        new_line = bl.rewrite(tag='v2.0.0')
        self.assertIn('TAG v2.0.0', new_line)
        self.assertTrue('BRANCH' not in new_line or 'BRANCH mybranch' not in new_line)

    def test_rewrite_with_new_git_repo(self):
        bl = BundleLine(SIMPLE_GIT_BUNDLE_LINE)
        new_repo = 'https://github.com/otherorg/otherrepo.git'
        new_line = bl.rewrite(git_repo=new_repo)
        self.assertIn(f'GIT "{new_repo}"', new_line)
        self.assertTrue('myrepo.git' not in new_line or new_repo in new_line)


ORIGINAL_CMAKE_FILE = """
cmake_minimum_required( VERSION 3.14 FATAL_ERROR )

project( jedi-bundle VERSION 8.0.0 LANGUAGES C CXX Fortran )

# Commented out bundle - should not be touched.
#ecbuild_bundle( PROJECT eckit    GIT "https://github.com/ecmwf/eckit.git" TAG 1.24.4 )
# Tag Bundle - will be disabled.
ecbuild_bundle( PROJECT gsibec   GIT "https://github.com/geos-esm/GSIbec" TAG 1.2.1 )
# Simple Git Bundle - will be updated to a tag.
ecbuild_bundle( PROJECT oops     GIT "https://github.com/jcsda-internal/oops.git"       BRANCH develop UPDATE )
# Flag enabled bundle (also whitespace padded bundle).
if(BUILD_RTTOV)
  ecbuild_bundle( PROJECT rttov    GIT "https://github.com/jcsda-internal/rttov.git" BRANCH develop UPDATE )
endif()
# Recursive Bundle - will also be updated to tag
ecbuild_bundle( PROJECT pyiri-jedi  GIT     "https://github.com/jcsda-internal/pyiri-jedi.git"    BRANCH develop  UPDATE  RECURSIVE )

ecbuild_bundle_finalize()

"""

TEST_RESULT_CMAKE_FILE = """
cmake_minimum_required( VERSION 3.14 FATAL_ERROR )

project( jedi-bundle VERSION 8.0.0 LANGUAGES C CXX Fortran )

# Commented out bundle - should not be touched.
#ecbuild_bundle( PROJECT eckit    GIT "https://github.com/ecmwf/eckit.git" TAG 1.24.4 )
# Tag Bundle - will be disabled.
# ecbuild_bundle( PROJECT gsibec   GIT "https://github.com/geos-esm/GSIbec" TAG 1.2.1 )
# Simple Git Bundle - will be updated to a tag.
ecbuild_bundle( PROJECT oops GIT "https://github.com/jcsda-internal/oops.git" TAG abc123 )
# Flag enabled bundle (also whitespace padded bundle).
if(BUILD_RTTOV)
  ecbuild_bundle( PROJECT rttov    GIT "https://github.com/jcsda-internal/rttov.git" BRANCH develop UPDATE )
endif()
# Recursive Bundle - will also be updated to tag
ecbuild_bundle( PROJECT pyiri-jedi GIT "https://github.com/jcsda-internal/pyiri-jedi.git" TAG 1337h4x0r RECURSIVE )

ecbuild_bundle_finalize()

"""


class TestCMakeFile(unittest.TestCase):

    def test_full_fidelity_rewrite(self):
        cmake_file = CMakeFile(ORIGINAL_CMAKE_FILE)
        fake_file = StringIO()
        cmake_file.basic_rewrite(fake_file)
        self.maxDiff = None
        written_text = fake_file.getvalue()
        print(written_text)
        self.assertMultiLineEqual(written_text, ORIGINAL_CMAKE_FILE)
    
    def test_rewrite_file_simple_tag(self):
        cmake_file = CMakeFile('# File header\necbuild_bundle( PROJECT oops     GIT "https://github.com/jcsda-internal/oops.git"       BRANCH develop UPDATE )\n')
        expected = '# File header\necbuild_bundle( PROJECT oops GIT "https://github.com/jcsda-internal/oops.git" TAG abc123 )\n'
        fake_file = StringIO()
        cmake_file.rewrite_whitelist(fake_file,
                                     enabled_bundles=set(['oops']),
                                     rewrite_rules={'oops': 'abc123'})
        found_bundle_line = cmake_file.bundle_line_names['oops']
        self.assertIsInstance(found_bundle_line, BundleLine)
        self.assertEqual(found_bundle_line.project_name, 'oops')
        self.assertMultiLineEqual(fake_file.getvalue(), expected)
    
    def test_rewrite_file_simple_disable(self):
        cmake_file = CMakeFile('# File header\necbuild_bundle( PROJECT oops GIT "https://github.com/jcsda-internal/oops.git" BRANCH develop UPDATE )\n')
        expected = '# File header\n# ecbuild_bundle( PROJECT oops GIT "https://github.com/jcsda-internal/oops.git" BRANCH develop UPDATE )\n'
        fake_file = StringIO()
        cmake_file.rewrite_whitelist(fake_file,
                                     enabled_bundles=set(),
                                     rewrite_rules={})
        found_bundle_line = cmake_file.bundle_line_names['oops']
        self.assertIsInstance(found_bundle_line, BundleLine)
        self.assertEqual(found_bundle_line.project_name, 'oops')
        self.assertMultiLineEqual(fake_file.getvalue(), expected)
        

    def test_rewrite_file(self):
        cmake_file = CMakeFile(ORIGINAL_CMAKE_FILE)
        fake_file = StringIO()
        cmake_file.rewrite_whitelist(fake_file,
                                     enabled_bundles=set(['oops', 'rttov', 'pyiri-jedi']),
                                     rewrite_rules={'oops': 'abc123', 'pyiri-jedi': '1337h4x0r'})
        self.maxDiff = None
        written_text = fake_file.getvalue()
        self.assertMultiLineEqual(written_text, TEST_RESULT_CMAKE_FILE)
    
    def test_get_github_urls(self):
        cmake_file = CMakeFile(ORIGINAL_CMAKE_FILE)
        urls = cmake_file.get_github_urls()
        expected = {
            'gsibec': 'https://github.com/geos-esm/GSIbec',
            'oops': 'https://github.com/jcsda-internal/oops.git',
            'rttov': 'https://github.com/jcsda-internal/rttov.git',
            'pyiri-jedi': 'https://github.com/jcsda-internal/pyiri-jedi.git'
        }
        self.assertDictEqual(urls, expected)


if __name__ == '__main__':
    unittest.main() 