"""
This is a tool to rewrite the CMakeLists.txt file by
parsing and rewriting the ecbuild_bundle() calls with
injected references

For reference, the general form of the ecbuild_bundle() call is:

   ecbuild_bundle( PROJECT <name>
                   STASH <repository> | GIT <giturl> | SOURCE <path>
                   [ BRANCH <gitbranch> | TAG <gittag> ]
                   [ UPDATE | NOREMOTE ]
                   [ MANUAL ]
                   [ RECURSIVE ] )
   Example:
       ecbuild_bundle( PROJECT myproject GIT "https://github.com/myorg/myrepo.git" BRANCH mybranch UPDATE RECURSIVE )

Since STASH is deprecated and is not supported by jedi-bundle or any
JEDI software this tool does not allow STASH to be used. All other
syntax is supported and GitHub repositories can be rewritten using the
CMakeFile class.
"""
from dataclasses import dataclass
from collections.abc import Container
from typing import Optional, Dict, Any
import re

from ci_action.library import github_client
@dataclass
class BundleLinePart:
    name: str
    # True if the component is an attribute like "UPDATE" or "RECURSIVE", False if it is a value.
    is_attribute: bool
    # The value of the component if it is not an attribute.
    value: str = None
    quote_char: str = str() # Defaults to empty string

    def __str__(self):
        #if self.name == "PROJECT":
        #    raise ValueError(f"PROJECT found!! {self.name} {self.quote_char}{self.value}{self.quote_char}... also isattrubute: {self.is_attribute}")
        if self.is_attribute:
            return f"{self.name}"
        else:
            return f"{self.name} {self.quote_char}{self.value}{self.quote_char}"


class BundleLine:

    _project_re = re.compile(r"^\s*ecbuild_bundle\(\s*PROJECT\s+([a-zA-Z0-9._-]*)\s+")
    _git_re = re.compile(r".*GIT\s+[\"']([a-zA-Z0-9/:._-]+)[\"']\s")
    _source_re = re.compile(r".*SOURCE\s+([a-zA-Z0-9/:._-]+)\s")
    _branch_or_tag_re = re.compile(r".*(BRANCH|TAG)\s+([a-zA-Z0-9._-]+)\s")
    _remote_rule_re = re.compile(r".*(UPDATE|NOREMOTE)\s")
    _manual_and_recursive_re = re.compile(r".*(MANUAL|RECURSIVE)\s")

    def __init__(self, content: str):
        self.content = content
        self.source_reference_type = None  # this will be set to "git" or "source"
        self.source_reference = None

        # During rewriting, we will check lines against rewrite rules stored as
        # a keyed value of "orgname/reponame" -> "updated_tag".
        self.github_org_repo_key = None

        self.version_ref_type = None  # this will be set to "branch" or "tag"
        self.version_ref = None

        self.remote_rule = None

        self.project = None
        self.components = []

        # Parse the project name
        match = self._project_re.match(content)
        if not match:
            raise ValueError(f"Invalid bundle; no project name\n {content}")
        self.project = BundleLinePart("PROJECT", False, match.group(1))
        self.project_name = match.group(1)

        # Parsing the git url or source path, these must be present and are mutually exclusive.
        # Parse the git url
        match = self._git_re.match(content)
        if match:
            self.source_reference = BundleLinePart("GIT", False, match.group(1), quote_char='"')
            self.source_reference_type = "git"
            git_uri = match.group(1).lower()
            if 'github.com' in git_uri:
                repo, org = github_client.get_repo_tuple_from_github_uri(git_uri)
                self.github_org_repo_key = f'{org}/{repo}'
        # Parse the source path
        match = self._source_re.match(content)
        if match and self.source_reference_type == "git":
            raise ValueError(f"Invalid bundle; git and source cannot both be present\n {content}")
        elif match:
            self.source_reference = BundleLinePart("SOURCE", False, match.group(1))
            self.source_reference_type = "source"
        
        if not self.source_reference_type:
            raise ValueError(f"Invalid bundle; no git or source\n {content}")
        
        # Parsing the branch or tag, these are optional (not used by source path) but if
        # present they are mutually exclusive..
        match = self._branch_or_tag_re.match(content)
        if match:
            self.version_ref_type = match.group(1).lower()
            self.version_ref = BundleLinePart(match.group(1), False, match.group(2))
        
        match = self._remote_rule_re.match(content)
        if match:
            self.remote_rule = BundleLinePart(match.group(1), True)

        # Parse both manual and recurive appending each as an attribute.
        match = self._manual_and_recursive_re.match(content)
        if match:
            for m in match.groups():
                if m:
                    self.components.append(BundleLinePart(m, True))

    def original_line(self):
        return self.content
    
    def disabled_line(self):
        return f"# {self.content}"
    
    def rewrite_original(self):
        """Render the original line with the original components; used for testing."""
        render_components =  [self.project, self.source_reference, self.version_ref, self.remote_rule] + self.components
        content = " ".join([str(c) for c in render_components])
        return f"ecbuild_bundle( {content} )"

    def rewrite(self, git_repo: str = None, branch: str = None, tag: str = None):
        if git_repo:
            source_reference = BundleLinePart("GIT", False, git_repo, quote_char='"')
        else:
            source_reference = self.source_reference

        # Set up local variables from the rewrite parameters.
        version_ref = None
        remote_rule = self.remote_rule
        if branch and tag:
            raise ValueError("branch and tag cannot both be present")
        if branch:
            version_ref = BundleLinePart("BRANCH", False, branch)
        if tag:
            version_ref = BundleLinePart("TAG", False, tag)
            remote_rule = None
        version_ref = version_ref or self.version_ref

        # Create list of components to render.
        render_components =  [self.project, source_reference, version_ref]
        if remote_rule:
            render_components.append(remote_rule)
        render_components += self.components
        content = " ".join([str(c) for c in render_components])
        return f"ecbuild_bundle( {content} )"


class CMakeFile:

    # Ecbuild bundle identfying regex; determines if a line has
    # an ecbuild_bundle() call that is active (excludes comments).
    _ecbuild_bundle_re = re.compile(r"\s*ecbuild_bundle\s*\(.*")

    def __init__(self, original_content: str):
        lines = original_content.splitlines()
        self.lines = []
        self.bundle_lines = {}
        self.bundle_line_names = {}
        for i, line in enumerate(lines):
            self.lines.append(line)
            if self._ecbuild_bundle_re.match(line):
                bundle_line = BundleLine(line)
                self.bundle_lines[i] = bundle_line  
                self.bundle_line_names[bundle_line.project_name] = bundle_line

    def get_github_urls(self):
        url_map = {}
        for bundle_name, bundle_line in self.bundle_line_names.items():
            if bundle_line.source_reference_type == "git" and 'github.com' in bundle_line.source_reference.value:
                url_map[bundle_name] = bundle_line.source_reference.value
        return url_map

    def _rewrite_file_implementation(
            self,
            file_object,
            enabled_bundles: Optional[Container[str]] = None,
            rewrite_rules: Optional[dict[str, str]] = None,
            build_group_commit_map: Optional[Dict[str, Dict[str, Any]]] = None,
            ):
        """Rewrite the CMakeFile object to the file_object.

        Args:
            file_object: The file to write to.
            enabled_bundles: A container of bundle names to enable.
            rewrite_rules: A mapping of bundle names to tags.
            build_group_commit_map: A mapping of "org/repo" keys to build group commit info.
                The build group commit info is a dictionary with the following structure:
                {
                    "name_key": "org/repo",
                    "uri": "https://github.com/org/repo.git",
                    "version_ref": {
                        "pr_id": 123,
                        "branch": "feature-branch",
                        "commit": "abcdef123456"
                    }
                }
        """
        if enabled_bundles is None:
            enabled_bundles = set()
        if rewrite_rules is None:
            rewrite_rules = {}
        if build_group_commit_map is None:
            build_group_commit_map = {}

        # Ensure rewrite_rules and build_group_commit_map are mutually exclusive
        if rewrite_rules and build_group_commit_map:
            raise ValueError("rewrite_rules and build_group_commit_map cannot both be provided")

        lines = []
        for i, line in enumerate(self.lines):
            # Each line that is not a bundle is left unchanged.
            if i not in self.bundle_lines:
                lines.append(line + '\n')
                continue

            bundle_line = self.bundle_lines[i]
            # If the line is a bundle we need to determine if/how it should be rewritten.
            if bundle_line.project_name not in enabled_bundles:
                lines.append(bundle_line.disabled_line() + '\n')
                continue

            # Check if this bundle matches a github org/repo key in the build group commit map
            if bundle_line.github_org_repo_key and bundle_line.github_org_repo_key in build_group_commit_map:
                # Use the commit hash as a tag
                commit_info = build_group_commit_map[bundle_line.github_org_repo_key]
                commit_hash = commit_info["version_ref"]["commit"]
                lines.append(bundle_line.rewrite(tag=commit_hash) + '\n')
                continue

            # If the line has a rewrite rule, use it.
            if bundle_line.project_name in rewrite_rules:
                tag = rewrite_rules[bundle_line.project_name]
                lines.append(bundle_line.rewrite(tag=tag) + '\n')
                continue

            # Finally if the line is enabled and has no rewrite, use the original line.
            lines.append(bundle_line.original_line() + '\n')
        
        # Write the lines to the file.
        file_object.writelines(lines)

    def basic_rewrite(self, file_object):
        """Rewrite the CMakeFile object to the file_object."""
        enabled_bundles = set(self.bundle_line_names.keys())
        self._rewrite_file_implementation(file_object, enabled_bundles=enabled_bundles)

    def rewrite_whitelist(self,
                          file_object,
                          enabled_bundles: Container[str],
                          rewrite_rules: dict[str, str]):
        """Rewrite the CMakeFile object to the file_object."""
        self._rewrite_file_implementation(file_object, enabled_bundles, rewrite_rules)

    def rewrite_blacklist(self,
                          file_object,
                          disabled_bundles: Container[str],
                          rewrite_rules: dict[str, str]):
        """Rewrite the CMakeFile object to the file_object."""
        enabled_bundles = set(self.bundle_line_names.keys())
        for bundle in disabled_bundles:
            enabled_bundles.discard(bundle)
        self._rewrite_file_implementation(file_object, enabled_bundles, rewrite_rules)

    def rewrite_build_group_whitelist(self,
                                      file_object,
                                      enabled_bundles: Container[str],
                                      build_group_commit_map: Dict[str, Dict[str, Any]]):
        """Rewrite the CMakeFile object to the file_object."""
        self._rewrite_file_implementation(file_object, enabled_bundles, build_group_commit_map=build_group_commit_map)

    def rewrite_build_group_blacklist(self,
                                      file_object,
                                      disabled_bundles: Container[str],
                                      build_group_commit_map: Dict[str, Dict[str, Any]]):
        """Rewrite the CMakeFile object to the file_object."""
        enabled_bundles = set(self.bundle_line_names.keys())
        for bundle in disabled_bundles:
            enabled_bundles.discard(bundle)
        self._rewrite_file_implementation(file_object, enabled_bundles, build_group_commit_map=build_group_commit_map)
