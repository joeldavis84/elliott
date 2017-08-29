import yaml
import shutil
import os
import filecmp
import re

from dockerfile_parse import DockerfileParser

from common import assert_file, assert_exec, assert_dir, Dir, recursive_overwrite
from model import Model, Missing

OIT_COMMENT_PREFIX = '#oit## '


class ImageMetadata(object):
    def __init__(self, runtime, dir, name):
        self.runtime = runtime
        self.dir = os.path.abspath(dir)
        self.config_path = os.path.join(self.dir, "config.yml")
        self.name = name

        runtime.verbose("Loading image metadata for %s from %s" % (name, self.config_path))

        assert_file(self.config_path, "Unable to find image configuration file")

        # Create an array of lines to eliminate the possibility of linefeed differences
        config_yml_lines = list(runtime.global_yaml_lines)
        with open(self.config_path, "r") as f:
            for line in f.readlines():
                config_yml_lines.append(line.rstrip())

        config_yml_content = "\n".join(config_yml_lines)
        runtime.verbose(config_yml_content)
        self.config = Model(yaml.load(config_yml_content))

        # Basic config validation. All images currently required to have a name in the metadata.
        # This is required because from.member uses these data to populate FROM in images.
        # It would be possible to query this data from the distgit Dockerflie label, but
        # not implementing this until we actually need it.
        assert (self.config.name is not Missing)

        self.type = "rpms"  # default type is rpms
        if self.config.repo.type is not Missing:
            self.type = self.config.repo.type

        self.qualified_name = "%s/%s" % (self.type, name)

        self._distgit_repo = None

    def distgit_repo(self):
        if self._distgit_repo is None:
            self._distgit_repo = DistGitRepo(self)
        return self._distgit_repo


class DistGitRepo(object):
    def __init__(self, metadata):
        self.metadata = metadata
        self.config = metadata.config
        self.runtime = metadata.runtime
        self.distgit_dir = None
        self.oit_comments = []
        # Initialize our distgit directory, if necessary
        self.clone_distgit(self.runtime.distgits_dir, self.runtime.distgit_branch)

    def clone_distgit(self, distgits_root_dir, distgit_branch):
        with Dir(distgits_root_dir):

            self.distgit_dir = os.path.abspath(os.path.join(os.getcwd(), self.metadata.name))
            if os.path.isdir(self.distgit_dir):
                self.runtime.verbose("Distgit directory already exists in working directory; skipping clone")
                return

            cmd_list = ["rhpkg"]

            if self.runtime.user is not None:
                cmd_list.append("--user=%s" % self.runtime.user)

            cmd_list.extend(["clone", self.metadata.qualified_name])


            self.runtime.info("Cloning distgit repository %s [branch:%s] into: %s" % (
                self.metadata.qualified_name, distgit_branch, self.distgit_dir))

            # Clone the distgit repository
            assert_exec(self.runtime, cmd_list)

            with Dir(self.distgit_dir):
                # Switch to the target branch
                assert_exec(self.runtime, ["rhpkg", "switch-branch", distgit_branch])

    def source_path(self):
        """
        :return: Returns the directory containing the source which should be used to populate distgit.
        """
        alias = self.config.content.source.alias

        # TODO: enable source to be something other than an alias?
        #       A fixed git URL and branch for example?
        if alias is Missing:
            raise IOError("Can't find source alias in image config: %s" % self.metadata.dir)

        if alias not in self.runtime.source_alias:
            raise IOError("Required source alias has not been registered [%s] for image config: %s" % (alias, self.metadata.dir))

        source_root = self.runtime.source_alias[alias]
        sub_path = self.config.content.source.path

        path = source_root
        if sub_path is not Missing:
            path = os.path.join(source_root, sub_path)

        assert_dir(path, "Unable to find path within source [%s] for config: %s" % (path, self.metadata.dir))
        return path

    def _merge_source(self):
        """
        Pulls source defined in content.source and overwrites most things in the distgit
        clone with content from that source.
        """

        # Clean up any files not special to the distgit repo
        for ent in os.listdir("."):

            # Do not delete anything that is hidden
            # protects .oit, .gitignore, others
            if ent.startswith("."):
                continue

            # Skip special files that aren't hidden
            if ent in ["additional-tags"]:
                continue

            # Otherwise, clean up the entry
            if os.path.isfile(ent):
                os.remove(ent)
            else:
                shutil.rmtree(ent)

        # Copy all files and overwrite where necessary
        recursive_overwrite(self.source_path(), self.distgit_dir)

        # See if the config is telling us a file other than "Dockerfile" defines the
        # distgit image content.
        dockerfile_name = self.config.content.source.dockerfile
        if dockerfile_name is not Missing and dockerfile_name != "Dockerfile":

            # Does a non-distgit Dockerfile already exists from copying source; remove if so
            if os.path.isfile("Dockerfile"):
                os.remove("Dockerfile")

            # Rename our distgit source Dockerfile appropriately
            os.rename(dockerfile_name, "Dockerilfe")

        # Clean up any extraneous Dockerfile.* that might be distractions (e.g. Dockerfile.centos)
        for ent in os.listdir("."):
            if ent.startswith("Dockerfile."):
                os.remove(ent)

        dockerfile_git_last_path = ".oit/Dockerfile.git.last"

        notify_owner = False

        # Do we have a copy of the last time we reconciled?
        if os.path.isfile(dockerfile_git_last_path):
            # See if it equals the Dockerfile we just pulled from source control
            if not filecmp.cmp(dockerfile_git_last_path, "Dockerfile", False):
                # Something has changed about the file in source control
                notify_owner = True
                # Update our .oit copy so we can detect the next change of this reconciliation
                os.remove(dockerfile_git_last_path)
                shutil.copy("Dockerfile", dockerfile_git_last_path)
        else:
            # We've never reconciled, so let the owner know about the change
            notify_owner = True

        # Leave a record for external processes that owners will need to notified.
        if notify_owner and self.config.owners is not Missing:
            owners_list = ", ".join(self.config.owners)
            self.runtime.add_record("dockerfile_notify", distgit=self.metadata.name, dockerfile=os.path.abspath("Dockerfile"), owners=owners_list)

        self.oit_comments.extend(
            ["The content of this file is managed from external source.",
             "Changes made directly in distgit will be lost during the next",
             "reconciliation process.",
             ""])

    def _run_modifications(self):
        """
        Interprets and applies content.source.modify steps in the image metadata.
        """

        with open("Dockerfile", 'r') as df:
            dockerfile_data = df.read()

        self.runtime.verbose("\nAbout to start modifying Dockerfile [%s]:\n%s\n" %
                             (self.metadata.name, dockerfile_data))

        for modification in self.config.content.source.modifications:
            if modification.action == "replace":
                match = modification.match
                assert (match is not Missing)
                replacement = modification.replacement
                assert (replacement is not Missing)
                pre = dockerfile_data
                dockerfile_data = pre.replace(match, replacement)
                if dockerfile_data == pre:
                    raise IOError("Replace (%s->%s) modification did not make a change to the Dockerfile content" % (match, replacement))
                self.runtime.verbose("\nPerformed string replace '%s' -> '%s':\n%s\n" %
                                     (match, replacement, dockerfile_data))
            else:
                raise IOError("Don't know how to perform modification action: %s" % modification.action)

        with open('Dockerfile', 'w') as df:
            df.write(dockerfile_data)

    def update_distgit_dir(self, version, release):

        # A collection of comment lines that will be included in the generated Dockerfile. They
        # will be prefix by the OIT_COMMENT_PREFIX and followed by newlines in the Dockerfile.
        self.oit_comments = [
            "This file is managed by the OpenShift Image tool: github.com/openshift/enterprise-images",
            "by the OpenShift Continuous Delivery team (#aos-cd-team on IRC).",
            ""
        ]

        with Dir(self.distgit_dir):

            # Make our metadata directory if it does not exist
            if not os.path.isdir(".oit"):
                os.mkdir(".oit")

            # If content.source is defined, pull in content from local source directory
            if self.config.content.source is not Missing:
                self._merge_source()
            else:
                self.oit_comments.extend([
                    "Some aspects of this file may be managed programmatically. For example, the image name, labels (version,",
                    "release, and other), and the base FROM. Changes made directly in distgit may be lost during the next",
                    "reconciliation.",
                    ""])

            # Source or not, we should find a Dockerfile in the root at this point or something is wrong
            assert_file("Dockerfile", "Unable to find Dockerfile in distgit root")

            if self.config.content.source.modifications is not Missing:
                self._run_modifications()

            dfp = DockerfileParser(path="Dockerfile")

            self.runtime.verbose("Dockerfile has parsed labels:")
            for k, v in dfp.labels.iteritems():
                self.runtime.verbose("  '%s'='%s'" % (k, v))

            # Set all labels in from config into the Dockerfile content
            if self.config.labels is not Missing:
                for k, v in self.config.labels.iteritems():
                    dfp.labels[k] = v

            # Set the image name
            dfp.labels["name"] = self.config.name

            # Set the distgit repo name
            dfp.labels["com.redhat.component"] = self.metadata.name

            # Does this image inherit from an image defined in a different distgit?
            if self.config["from"].member is not Missing:
                from_image_metadata = self.runtime.resolve_image(self.config["from"].member)
                # Everything in the group is going to be built with the same version and release,
                # so just assume it will exist with the version-release we are using for this
                # repo.
                dfp.baseimage = "%s:%s-%s" % (from_image_metadata.config.name, version, release)

            # Is this image FROM another literal image name:tag?
            if self.config["from"].image is not Missing:
                dfp.baseimage = self.config["from"].image

            if self.config["from"].stream is not Missing:
                stream = self.runtime.resolve_stream(self.config["from"].stream)
                # TODO: implement expriring images?
                dfp.baseimage = stream.image

            # Set image name in case it has changed
            dfp.labels["name"] = self.config.name

            # Set version and release fields
            dfp.labels["version"] = version
            dfp.labels["release"] = release

            # Remove any programmatic oit comments from previous management
            df_lines = dfp.content.splitlines(False)
            df_lines = [line for line in df_lines if not line.strip().startswith(OIT_COMMENT_PREFIX)]

            df_content = "\n".join(df_lines)

            with open('Dockerfile', 'w') as df:
                for comment in self.oit_comments:
                    df.write("%s%s\n" % (OIT_COMMENT_PREFIX, comment))
                df.write(df_content)