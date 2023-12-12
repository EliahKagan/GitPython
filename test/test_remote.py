# Copyright (C) 2008, 2009 Michael Trier (mtrier@gmail.com) and contributors
#
# This module is part of GitPython and is released under the
# 3-Clause BSD License: https://opensource.org/license/bsd-3-clause/

import os
import os.path as osp
from pathlib import Path
import random
import tempfile
from unittest import skipIf

import pytest

from git import (
    Commit,
    FetchInfo,
    GitCommandError,
    Head,
    PushInfo,
    Reference,
    Remote,
    RemoteProgress,
    RemoteReference,
    SymbolicReference,
    TagReference,
)
from git.cmd import Git
from git.exc import UnsafeOptionError, UnsafeProtocolError
from git.util import rmtree, HIDE_WINDOWS_FREEZE_ERRORS, IterableList
from test.lib import (
    GIT_DAEMON_PORT,
    TestBase,
    fixture,
    with_rw_and_rw_remote_repo,
    with_rw_repo,
)


# Make sure we have repeatable results.
random.seed(0)


class TestRemoteProgress(RemoteProgress):
    __slots__ = ("_seen_lines", "_stages_per_op", "_num_progress_messages")

    def __init__(self):
        super().__init__()
        self._seen_lines = []
        self._stages_per_op = {}
        self._num_progress_messages = 0

    def _parse_progress_line(self, line):
        # We may remove the line later if it is dropped.
        # Keep it for debugging.
        self._seen_lines.append(line)
        rval = super()._parse_progress_line(line)
        return rval

    def line_dropped(self, line):
        try:
            self._seen_lines.remove(line)
        except ValueError:
            pass

    def update(self, op_code, cur_count, max_count=None, message=""):
        # Check each stage only comes once.
        op_id = op_code & self.OP_MASK
        assert op_id in (self.COUNTING, self.COMPRESSING, self.WRITING)

        if op_code & self.WRITING > 0:
            if op_code & self.BEGIN > 0:
                assert not message, "should not have message when remote begins writing"
            elif op_code & self.END > 0:
                assert message
                assert not message.startswith(", "), "Sanitize progress messages: '%s'" % message
                assert not message.endswith(", "), "Sanitize progress messages: '%s'" % message

        self._stages_per_op.setdefault(op_id, 0)
        self._stages_per_op[op_id] = self._stages_per_op[op_id] | (op_code & self.STAGE_MASK)

        if op_code & (self.WRITING | self.END) == (self.WRITING | self.END):
            assert message
        # END check we get message

        self._num_progress_messages += 1

    def make_assertion(self):
        # We don't always receive messages.
        if not self._seen_lines:
            return

        # Sometimes objects are not compressed which is okay.
        assert len(self._seen_ops) in (2, 3), len(self._seen_ops)
        assert self._stages_per_op

        # Must have seen all stages.
        for _op, stages in self._stages_per_op.items():
            assert stages & self.STAGE_MASK == self.STAGE_MASK
        # END for each op/stage

    def assert_received_message(self):
        assert self._num_progress_messages


class TestRemote(TestBase):
    def tearDown(self):
        import gc

        gc.collect()

    def _print_fetchhead(self, repo):
        with open(osp.join(repo.git_dir, "FETCH_HEAD")):
            pass

    def _do_test_fetch_result(self, results, remote):
        # self._print_fetchhead(remote.repo)
        self.assertGreater(len(results), 0)
        self.assertIsInstance(results[0], FetchInfo)
        for info in results:
            self.assertIsInstance(info.note, str)
            if isinstance(info.ref, Reference):
                self.assertTrue(info.flags)
            # END reference type flags handling
            self.assertIsInstance(info.ref, (SymbolicReference, Reference))
            if info.flags & (info.FORCED_UPDATE | info.FAST_FORWARD):
                self.assertIsInstance(info.old_commit, Commit)
            else:
                self.assertIsNone(info.old_commit)
            # END forced update checking
        # END for each info

    def _do_test_push_result(self, results, remote):
        self.assertIsInstance(results, list)
        self.assertIsInstance(results, IterableList)

        self.assertGreater(len(results), 0)
        self.assertIsInstance(results[0], PushInfo)
        for info in results:
            self.assertTrue(info.flags)
            self.assertIsInstance(info.summary, str)
            if info.old_commit is not None:
                self.assertIsInstance(info.old_commit, Commit)
            if info.flags & info.ERROR:
                has_one = False
                for bitflag in (
                    info.REJECTED,
                    info.REMOTE_REJECTED,
                    info.REMOTE_FAILURE,
                ):
                    has_one |= bool(info.flags & bitflag)
                # END for each bitflag
                self.assertTrue(has_one)
            else:
                # There must be a remote commit.
                if info.flags & info.DELETED == 0:
                    self.assertIsInstance(info.local_ref, Reference)
                else:
                    self.assertIsNone(info.local_ref)
                self.assertIn(type(info.remote_ref), (TagReference, RemoteReference))
            # END error checking
        # END for each info

        if any(info.flags & info.ERROR for info in results):
            self.assertRaises(GitCommandError, results.raise_if_error)
        else:
            # No errors, so this should do nothing.
            results.raise_if_error()

    def _do_test_fetch_info(self, repo):
        self.assertRaises(ValueError, FetchInfo._from_line, repo, "nonsense", "")
        self.assertRaises(
            ValueError,
            FetchInfo._from_line,
            repo,
            "? [up to date]      0.1.7RC    -> origin/0.1.7RC",
            "",
        )

    def _commit_random_file(self, repo):
        """Create a file with a random name and random data and commit it to a repo.

        :return: The committed absolute file path.
        """
        index = repo.index
        new_file = self._make_file(osp.basename(tempfile.mktemp()), str(random.random()), repo)
        index.add([new_file])
        index.commit("Committing %s" % new_file)
        return new_file

    def _do_test_fetch(self, remote, rw_repo, remote_repo, **kwargs):
        """Specialized fetch testing to de-clutter the main test."""
        self._do_test_fetch_info(rw_repo)

        def fetch_and_test(remote, **kwargs):
            progress = TestRemoteProgress()
            kwargs["progress"] = progress
            res = remote.fetch(**kwargs)
            progress.make_assertion()
            self._do_test_fetch_result(res, remote)
            return res

        # END fetch and check

        def get_info(res, remote, name):
            return res["%s/%s" % (remote, name)]

        # Put remote head to master as it is guaranteed to exist.
        remote_repo.head.reference = remote_repo.heads.master

        res = fetch_and_test(remote, **kwargs)
        # All up to date.
        for info in res:
            self.assertTrue(info.flags & info.HEAD_UPTODATE)

        # Rewind remote head to trigger rejection.
        # index must be false as remote is a bare repo.
        rhead = remote_repo.head
        remote_commit = rhead.commit
        rhead.reset("HEAD~2", index=False)
        res = fetch_and_test(remote)
        mkey = "%s/%s" % (remote, "master")
        master_info = res[mkey]
        self.assertTrue(master_info.flags & FetchInfo.FORCED_UPDATE)
        self.assertIsNotNone(master_info.note)

        # Normal fast forward - set head back to previous one.
        rhead.commit = remote_commit
        res = fetch_and_test(remote)
        self.assertTrue(res[mkey].flags & FetchInfo.FAST_FORWARD)

        # New remote branch.
        new_remote_branch = Head.create(remote_repo, "new_branch")
        res = fetch_and_test(remote)
        new_branch_info = get_info(res, remote, new_remote_branch)
        self.assertTrue(new_branch_info.flags & FetchInfo.NEW_HEAD)

        # Remote branch rename (causes creation of a new one locally).
        new_remote_branch.rename("other_branch_name")
        res = fetch_and_test(remote)
        other_branch_info = get_info(res, remote, new_remote_branch)
        self.assertEqual(other_branch_info.ref.commit, new_branch_info.ref.commit)

        # Remove new branch.
        Head.delete(new_remote_branch.repo, new_remote_branch)
        res = fetch_and_test(remote)
        # Deleted remote will not be fetched.
        self.assertRaises(IndexError, get_info, res, remote, new_remote_branch)

        # Prune stale tracking branches.
        stale_refs = remote.stale_refs
        self.assertEqual(len(stale_refs), 2)
        self.assertIsInstance(stale_refs[0], RemoteReference)
        RemoteReference.delete(rw_repo, *stale_refs)

        # Test single branch fetch with refspec including target remote.
        res = fetch_and_test(remote, refspec="master:refs/remotes/%s/master" % remote)
        self.assertEqual(len(res), 1)
        self.assertTrue(get_info(res, remote, "master"))

        # ...with respec and no target.
        res = fetch_and_test(remote, refspec="master")
        self.assertEqual(len(res), 1)

        # ...multiple refspecs...works, but git command returns with error if one ref is
        # wrong without doing anything. This is new in later binaries.
        # res = fetch_and_test(remote, refspec=['master', 'fred'])
        # self.assertEqual(len(res), 1)

        # Add new tag reference.
        rtag = TagReference.create(remote_repo, "1.0-RV_hello.there")
        res = fetch_and_test(remote, tags=True)
        tinfo = res[str(rtag)]
        self.assertIsInstance(tinfo.ref, TagReference)
        self.assertEqual(tinfo.ref.commit, rtag.commit)
        self.assertTrue(tinfo.flags & tinfo.NEW_TAG)

        # Adjust the local tag commit.
        Reference.set_object(rtag, rhead.commit.parents[0].parents[0])

        # As of git 2.20 one cannot clobber local tags that have changed without
        # specifying --force, and the test assumes you can clobber, so...
        force = None
        if rw_repo.git.version_info[:2] >= (2, 20):
            force = True
        res = fetch_and_test(remote, tags=True, force=force)
        tinfo = res[str(rtag)]
        self.assertEqual(tinfo.commit, rtag.commit)
        self.assertTrue(tinfo.flags & tinfo.TAG_UPDATE)

        # Delete remote tag - local one will stay.
        TagReference.delete(remote_repo, rtag)
        res = fetch_and_test(remote, tags=True)
        self.assertRaises(IndexError, get_info, res, remote, str(rtag))

        # Provoke to receive actual objects to see what kind of output we have to
        # expect. For that we need a remote transport protocol.
        # Create a new UN-shared repo and fetch into it after we pushed a change
        # to the shared repo.
        other_repo_dir = tempfile.mktemp("other_repo")
        # Must clone with a local path for the repo implementation not to freak out
        # as it wants local paths only (which I can understand).
        other_repo = remote_repo.clone(other_repo_dir, shared=False)
        remote_repo_url = osp.basename(remote_repo.git_dir)  # git-daemon runs with appropriate `--base-path`.
        remote_repo_url = Git.polish_url("git://localhost:%s/%s" % (GIT_DAEMON_PORT, remote_repo_url))

        # Put origin to git-url.
        other_origin = other_repo.remotes.origin
        with other_origin.config_writer as cw:
            cw.set("url", remote_repo_url)
        # It automatically creates alternates as remote_repo is shared as well.
        # It will use the transport though and ignore alternates when fetching.
        # assert not other_repo.alternates  # this would fail

        # Ensure we are in the right state.
        rw_repo.head.reset(remote.refs.master, working_tree=True)
        try:
            self._commit_random_file(rw_repo)
            remote.push(rw_repo.head.reference)

            # Here I would expect to see remote-information about packing
            # objects and so on. Unfortunately, this does not happen
            # if we are redirecting the output - git explicitly checks for this
            # and only provides progress information to ttys.
            res = fetch_and_test(other_origin)
        finally:
            rmtree(other_repo_dir)
        # END test and cleanup

    def _assert_push_and_pull(self, remote, rw_repo, remote_repo):
        # Push our changes.
        lhead = rw_repo.head
        # Ensure we are on master and it is checked out where the remote is.
        try:
            lhead.reference = rw_repo.heads.master
        except AttributeError:
            # If the author is on a non-master branch, the clones might not have
            # a local master yet. We simply create it.
            lhead.reference = rw_repo.create_head("master")
        # END master handling
        lhead.reset(remote.refs.master, working_tree=True)

        # Push without spec should fail (without further configuration)
        # well, works nicely
        # self.assertRaises(GitCommandError, remote.push)

        # Simple file push.
        self._commit_random_file(rw_repo)
        progress = TestRemoteProgress()
        res = remote.push(lhead.reference, progress)
        self.assertIsInstance(res, list)
        self._do_test_push_result(res, remote)
        progress.make_assertion()

        # Rejected - undo last commit.
        lhead.reset("HEAD~1")
        res = remote.push(lhead.reference)
        self.assertTrue(res[0].flags & PushInfo.ERROR)
        self.assertTrue(res[0].flags & PushInfo.REJECTED)
        self._do_test_push_result(res, remote)

        # Force rejected pull.
        res = remote.push("+%s" % lhead.reference)
        self.assertEqual(res[0].flags & PushInfo.ERROR, 0)
        self.assertTrue(res[0].flags & PushInfo.FORCED_UPDATE)
        self._do_test_push_result(res, remote)

        # Invalid refspec.
        self.assertRaises(GitCommandError, remote.push, "hellothere")

        # Push new tags.
        progress = TestRemoteProgress()
        to_be_updated = "my_tag.1.0RV"
        new_tag = TagReference.create(rw_repo, to_be_updated)  # @UnusedVariable
        other_tag = TagReference.create(rw_repo, "my_obj_tag.2.1aRV", logmsg="my message")
        res = remote.push(progress=progress, tags=True)
        self.assertTrue(res[-1].flags & PushInfo.NEW_TAG)
        progress.make_assertion()
        self._do_test_push_result(res, remote)

        # Update push new tags.
        # Rejection is default.
        new_tag = TagReference.create(rw_repo, to_be_updated, reference="HEAD~1", force=True)
        res = remote.push(tags=True)
        self._do_test_push_result(res, remote)
        self.assertTrue(res[-1].flags & PushInfo.REJECTED)
        self.assertTrue(res[-1].flags & PushInfo.ERROR)

        # Force push this tag.
        res = remote.push("+%s" % new_tag.path)
        self.assertEqual(res[-1].flags & PushInfo.ERROR, 0)
        self.assertTrue(res[-1].flags & PushInfo.FORCED_UPDATE)

        # Delete tag - have to do it using refspec.
        res = remote.push(":%s" % new_tag.path)
        self._do_test_push_result(res, remote)
        self.assertTrue(res[0].flags & PushInfo.DELETED)
        # Currently progress is not properly transferred, especially not using
        # the git daemon.
        # progress.assert_received_message()

        # Push new branch.
        new_head = Head.create(rw_repo, "my_new_branch")
        progress = TestRemoteProgress()
        res = remote.push(new_head, progress)
        self.assertGreater(len(res), 0)
        self.assertTrue(res[0].flags & PushInfo.NEW_HEAD)
        progress.make_assertion()
        self._do_test_push_result(res, remote)

        # Rejected stale delete.
        force_with_lease = "%s:0000000000000000000000000000000000000000" % new_head.path
        res = remote.push(":%s" % new_head.path, force_with_lease=force_with_lease)
        self.assertTrue(res[0].flags & PushInfo.ERROR)
        self.assertTrue(res[0].flags & PushInfo.REJECTED)
        self.assertIsNone(res[0].local_ref)
        self._do_test_push_result(res, remote)

        # Delete new branch on the remote end and locally.
        res = remote.push(":%s" % new_head.path)
        self._do_test_push_result(res, remote)
        Head.delete(rw_repo, new_head)
        self.assertTrue(res[-1].flags & PushInfo.DELETED)

        # --all
        res = remote.push(all=True)
        self._do_test_push_result(res, remote)

        remote.pull("master", kill_after_timeout=10.0)

        # Cleanup - delete created tags and branches as we are in an inner loop on
        # the same repository.
        TagReference.delete(rw_repo, new_tag, other_tag)
        remote.push(":%s" % other_tag.path, kill_after_timeout=10.0)

    @skipIf(HIDE_WINDOWS_FREEZE_ERRORS, "FIXME: Freezes!")
    @with_rw_and_rw_remote_repo("0.1.6")
    def test_base(self, rw_repo, remote_repo):
        num_remotes = 0
        remote_set = set()
        ran_fetch_test = False

        for remote in rw_repo.remotes:
            num_remotes += 1
            self.assertEqual(remote, remote)
            self.assertNotEqual(str(remote), repr(remote))
            remote_set.add(remote)
            remote_set.add(remote)  # Should already exist.
            # REFS
            refs = remote.refs
            self.assertTrue(refs)
            for ref in refs:
                self.assertEqual(ref.remote_name, remote.name)
                self.assertTrue(ref.remote_head)
            # END for each ref

            # OPTIONS
            # Cannot use 'fetch' key anymore as it is now a method.
            for opt in ("url",):
                val = getattr(remote, opt)
                reader = remote.config_reader
                assert reader.get(opt) == val
                assert reader.get_value(opt, None) == val

                # Unable to write with a reader.
                self.assertRaises(IOError, reader.set, opt, "test")

                # Change value.
                with remote.config_writer as writer:
                    new_val = "myval"
                    writer.set(opt, new_val)
                    assert writer.get(opt) == new_val
                    writer.set(opt, val)
                    assert writer.get(opt) == val
                assert getattr(remote, opt) == val
            # END for each default option key

            # RENAME
            other_name = "totally_other_name"
            prev_name = remote.name
            self.assertEqual(remote.rename(other_name), remote)
            self.assertNotEqual(prev_name, remote.name)
            # Multiple times.
            for _ in range(2):
                self.assertEqual(remote.rename(prev_name).name, prev_name)
            # END for each rename ( back to prev_name )

            # PUSH/PULL TESTING
            self._assert_push_and_pull(remote, rw_repo, remote_repo)

            # FETCH TESTING
            # Only for remotes - local cases are the same or less complicated
            # as additional progress information will never be emitted.
            if remote.name == "daemon_origin":
                self._do_test_fetch(remote, rw_repo, remote_repo, kill_after_timeout=10.0)
                ran_fetch_test = True
            # END fetch test

            remote.update()
        # END for each remote

        self.assertTrue(ran_fetch_test)
        self.assertTrue(num_remotes)
        self.assertEqual(num_remotes, len(remote_set))

        origin = rw_repo.remote("origin")
        assert origin == rw_repo.remotes.origin

        # Verify we can handle prunes when fetching.
        # stderr lines look like this:  x [deleted]         (none)     -> origin/experiment-2012
        # These should just be skipped.
        # If we don't have a manual checkout, we can't actually assume there are any non-master branches.
        remote_repo.create_head("myone_for_deletion")
        # Get the branch - to be pruned later
        origin.fetch()

        num_deleted = False
        for branch in remote_repo.heads:
            if branch.name != "master":
                branch.delete(remote_repo, branch, force=True)
                num_deleted += 1
        # END for each branch
        self.assertGreater(num_deleted, 0)
        self.assertEqual(
            len(rw_repo.remotes.origin.fetch(prune=True)),
            1,
            "deleted everything but master",
        )

    @with_rw_repo("HEAD", bare=True)
    def test_creation_and_removal(self, bare_rw_repo):
        new_name = "test_new_one"
        arg_list = (new_name, "git@server:hello.git")
        remote = Remote.create(bare_rw_repo, *arg_list)
        self.assertEqual(remote.name, "test_new_one")
        self.assertIn(remote, bare_rw_repo.remotes)
        self.assertTrue(remote.exists())

        # Create same one again.
        self.assertRaises(GitCommandError, Remote.create, bare_rw_repo, *arg_list)

        Remote.remove(bare_rw_repo, new_name)
        self.assertTrue(remote.exists())  # We still have a cache that doesn't know we were deleted by name.
        remote._clear_cache()
        assert not remote.exists()  # Cache should be renewed now. This is an issue...

        for remote in bare_rw_repo.remotes:
            if remote.name == new_name:
                raise AssertionError("Remote removal failed")
            # END if deleted remote matches existing remote's name
        # END for each remote

        # Issue #262 - the next call would fail if bug wasn't fixed.
        bare_rw_repo.create_remote("bogus", "/bogus/path", mirror="push")

    def test_fetch_info(self):
        # Ensure we can handle remote-tracking branches.
        fetch_info_line_fmt = "c437ee5deb8d00cf02f03720693e4c802e99f390	not-for-merge	%s '0.3' of "
        fetch_info_line_fmt += "git://github.com/gitpython-developers/GitPython"
        remote_info_line_fmt = "* [new branch]      nomatter     -> %s"

        self.assertRaises(
            ValueError,
            FetchInfo._from_line,
            self.rorepo,
            remote_info_line_fmt % "refs/something/branch",
            "269c498e56feb93e408ed4558c8138d750de8893\t\t/Users/ben/test/foo\n",
        )

        fi = FetchInfo._from_line(
            self.rorepo,
            remote_info_line_fmt % "local/master",
            fetch_info_line_fmt % "remote-tracking branch",
        )
        assert not fi.ref.is_valid()
        self.assertEqual(fi.ref.name, "local/master")

        # Handles non-default refspecs: One can specify a different path in refs/remotes
        # or a special path just in refs/something for instance.

        fi = FetchInfo._from_line(
            self.rorepo,
            remote_info_line_fmt % "subdir/tagname",
            fetch_info_line_fmt % "tag",
        )

        self.assertIsInstance(fi.ref, TagReference)
        assert fi.ref.path.startswith("refs/tags"), fi.ref.path

        # It could be in a remote directory though.
        fi = FetchInfo._from_line(
            self.rorepo,
            remote_info_line_fmt % "remotename/tags/tagname",
            fetch_info_line_fmt % "tag",
        )

        self.assertIsInstance(fi.ref, TagReference)
        assert fi.ref.path.startswith("refs/remotes/"), fi.ref.path

        # It can also be anywhere!
        tag_path = "refs/something/remotename/tags/tagname"
        fi = FetchInfo._from_line(self.rorepo, remote_info_line_fmt % tag_path, fetch_info_line_fmt % "tag")

        self.assertIsInstance(fi.ref, TagReference)
        self.assertEqual(fi.ref.path, tag_path)

        # Branches default to refs/remotes.
        fi = FetchInfo._from_line(
            self.rorepo,
            remote_info_line_fmt % "remotename/branch",
            fetch_info_line_fmt % "branch",
        )

        self.assertIsInstance(fi.ref, RemoteReference)
        self.assertEqual(fi.ref.remote_name, "remotename")

        # But you can force it anywhere, in which case we only have a references.
        fi = FetchInfo._from_line(
            self.rorepo,
            remote_info_line_fmt % "refs/something/branch",
            fetch_info_line_fmt % "branch",
        )

        assert type(fi.ref) is Reference, type(fi.ref)
        self.assertEqual(fi.ref.path, "refs/something/branch")

    def test_uncommon_branch_names(self):
        stderr_lines = fixture("uncommon_branch_prefix_stderr").decode("ascii").splitlines()
        fetch_lines = fixture("uncommon_branch_prefix_FETCH_HEAD").decode("ascii").splitlines()

        # The contents of the files above must be fetched with a custom refspec:
        # +refs/pull/*:refs/heads/pull/*
        res = [
            FetchInfo._from_line("ShouldntMatterRepo", stderr, fetch_line)
            for stderr, fetch_line in zip(stderr_lines, fetch_lines)
        ]
        self.assertGreater(len(res), 0)
        self.assertEqual(res[0].remote_ref_path, "refs/pull/1/head")
        self.assertEqual(res[0].ref.path, "refs/heads/pull/1/head")
        self.assertIsInstance(res[0].ref, Head)

    @with_rw_repo("HEAD", bare=False)
    def test_multiple_urls(self, rw_repo):
        # Test addresses.
        test1 = "https://github.com/gitpython-developers/GitPython"
        test2 = "https://github.com/gitpython-developers/gitdb"
        test3 = "https://github.com/gitpython-developers/smmap"

        remote = rw_repo.remotes[0]
        # Test setting a single URL.
        remote.set_url(test1)
        self.assertEqual(list(remote.urls), [test1])

        # Test replacing that single URL.
        remote.set_url(test1)
        self.assertEqual(list(remote.urls), [test1])
        # Test adding new URLs.
        remote.set_url(test2, add=True)
        self.assertEqual(list(remote.urls), [test1, test2])
        remote.set_url(test3, add=True)
        self.assertEqual(list(remote.urls), [test1, test2, test3])
        # Test removing a URL.
        remote.set_url(test2, delete=True)
        self.assertEqual(list(remote.urls), [test1, test3])
        # Test changing a URL.
        remote.set_url(test2, test3)
        self.assertEqual(list(remote.urls), [test1, test2])

        # will raise: fatal: --add --delete doesn't make sense
        self.assertRaises(GitCommandError, remote.set_url, test2, add=True, delete=True)

        # Test on another remote, with the add/delete URL.
        remote = rw_repo.create_remote("another", url=test1)
        remote.add_url(test2)
        self.assertEqual(list(remote.urls), [test1, test2])
        remote.add_url(test3)
        self.assertEqual(list(remote.urls), [test1, test2, test3])
        # Test removing all the URLs.
        remote.delete_url(test2)
        self.assertEqual(list(remote.urls), [test1, test3])
        remote.delete_url(test1)
        self.assertEqual(list(remote.urls), [test3])
        # Will raise fatal: Will not delete all non-push URLs.
        self.assertRaises(GitCommandError, remote.delete_url, test3)

    def test_fetch_error(self):
        rem = self.rorepo.remote("origin")
        with self.assertRaisesRegex(GitCommandError, "[Cc]ouldn't find remote ref __BAD_REF__"):
            rem.fetch("__BAD_REF__")

    @with_rw_repo("0.1.6", bare=False)
    def test_push_error(self, repo):
        rem = repo.remote("origin")
        with self.assertRaisesRegex(GitCommandError, "src refspec __BAD_REF__ does not match any"):
            rem.push("__BAD_REF__")

    @with_rw_repo("HEAD")
    def test_set_unsafe_url(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            remote = rw_repo.remote("origin")
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::17/foo",
            ]
            for url in urls:
                with self.assertRaises(UnsafeProtocolError):
                    remote.set_url(url)
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_set_unsafe_url_allowed(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            remote = rw_repo.remote("origin")
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::17/foo",
            ]
            for url in urls:
                remote.set_url(url, allow_unsafe_protocols=True)
                assert list(remote.urls)[-1] == url
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_add_unsafe_url(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            remote = rw_repo.remote("origin")
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::17/foo",
            ]
            for url in urls:
                with self.assertRaises(UnsafeProtocolError):
                    remote.add_url(url)
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_add_unsafe_url_allowed(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            remote = rw_repo.remote("origin")
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::17/foo",
            ]
            for url in urls:
                remote.add_url(url, allow_unsafe_protocols=True)
                assert list(remote.urls)[-1] == url
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_create_remote_unsafe_url(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::17/foo",
            ]
            for url in urls:
                with self.assertRaises(UnsafeProtocolError):
                    Remote.create(rw_repo, "origin", url)
                assert not tmp_file.exists()

    @pytest.mark.xfail(
        os.name == "nt",
        reason=R"Multiple '\' instead of '/' in remote.url make it differ from expected value",
        raises=AssertionError,
    )
    @with_rw_repo("HEAD")
    def test_create_remote_unsafe_url_allowed(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::17/foo",
            ]
            for i, url in enumerate(urls):
                remote = Remote.create(rw_repo, f"origin{i}", url, allow_unsafe_protocols=True)
                assert remote.url == url
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_fetch_unsafe_url(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            remote = rw_repo.remote("origin")
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::17/foo",
            ]
            for url in urls:
                with self.assertRaises(UnsafeProtocolError):
                    remote.fetch(url)
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_fetch_unsafe_url_allowed(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            remote = rw_repo.remote("origin")
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::17/foo",
            ]
            for url in urls:
                # The URL will be allowed into the command, but the command will
                # fail since we don't have that protocol enabled in the Git config file.
                with self.assertRaises(GitCommandError):
                    remote.fetch(url, allow_unsafe_protocols=True)
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_fetch_unsafe_options(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            remote = rw_repo.remote("origin")
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            unsafe_options = [{"upload-pack": f"touch {tmp_file}"}]
            for unsafe_option in unsafe_options:
                with self.assertRaises(UnsafeOptionError):
                    remote.fetch(**unsafe_option)
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_fetch_unsafe_options_allowed(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            remote = rw_repo.remote("origin")
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            unsafe_options = [{"upload-pack": f"touch {tmp_file}"}]
            for unsafe_option in unsafe_options:
                # The options will be allowed, but the command will fail.
                assert not tmp_file.exists()
                with self.assertRaises(GitCommandError):
                    remote.fetch(**unsafe_option, allow_unsafe_options=True)
                assert tmp_file.exists()
                tmp_file.unlink()

    @with_rw_repo("HEAD")
    def test_pull_unsafe_url(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            remote = rw_repo.remote("origin")
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::17/foo",
            ]
            for url in urls:
                with self.assertRaises(UnsafeProtocolError):
                    remote.pull(url)
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_pull_unsafe_url_allowed(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            remote = rw_repo.remote("origin")
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::17/foo",
            ]
            for url in urls:
                # The URL will be allowed into the command, but the command will
                # fail since we don't have that protocol enabled in the Git config file.
                with self.assertRaises(GitCommandError):
                    remote.pull(url, allow_unsafe_protocols=True)
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_pull_unsafe_options(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            remote = rw_repo.remote("origin")
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            unsafe_options = [{"upload-pack": f"touch {tmp_file}"}]
            for unsafe_option in unsafe_options:
                with self.assertRaises(UnsafeOptionError):
                    remote.pull(**unsafe_option)
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_pull_unsafe_options_allowed(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            remote = rw_repo.remote("origin")
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            unsafe_options = [{"upload-pack": f"touch {tmp_file}"}]
            for unsafe_option in unsafe_options:
                # The options will be allowed, but the command will fail.
                assert not tmp_file.exists()
                with self.assertRaises(GitCommandError):
                    remote.pull(**unsafe_option, allow_unsafe_options=True)
                assert tmp_file.exists()
                tmp_file.unlink()

    @with_rw_repo("HEAD")
    def test_push_unsafe_url(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            remote = rw_repo.remote("origin")
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::17/foo",
            ]
            for url in urls:
                with self.assertRaises(UnsafeProtocolError):
                    remote.push(url)
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_push_unsafe_url_allowed(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            remote = rw_repo.remote("origin")
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::17/foo",
            ]
            for url in urls:
                # The URL will be allowed into the command, but the command will
                # fail since we don't have that protocol enabled in the Git config file.
                with self.assertRaises(GitCommandError):
                    remote.push(url, allow_unsafe_protocols=True)
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_push_unsafe_options(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            remote = rw_repo.remote("origin")
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            unsafe_options = [
                {
                    "receive-pack": f"touch {tmp_file}",
                    "exec": f"touch {tmp_file}",
                }
            ]
            for unsafe_option in unsafe_options:
                assert not tmp_file.exists()
                with self.assertRaises(UnsafeOptionError):
                    remote.push(**unsafe_option)
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_push_unsafe_options_allowed(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            remote = rw_repo.remote("origin")
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            unsafe_options = [
                {
                    "receive-pack": f"touch {tmp_file}",
                    "exec": f"touch {tmp_file}",
                }
            ]
            for unsafe_option in unsafe_options:
                # The options will be allowed, but the command will fail.
                assert not tmp_file.exists()
                with self.assertRaises(GitCommandError):
                    remote.push(**unsafe_option, allow_unsafe_options=True)
                assert tmp_file.exists()
                tmp_file.unlink()


class TestTimeouts(TestBase):
    @with_rw_repo("HEAD", bare=False)
    def test_timeout_funcs(self, repo):
        # Force error code to prevent a race condition if the python thread is slow.
        default = Git.AutoInterrupt._status_code_if_terminate
        Git.AutoInterrupt._status_code_if_terminate = -15
        for function in ["pull", "fetch"]:  # Can't get push to time out.
            f = getattr(repo.remotes.origin, function)
            assert f is not None  # Make sure these functions exist.
            _ = f()  # Make sure the function runs.
            with pytest.raises(GitCommandError, match="kill_after_timeout=0 s"):
                f(kill_after_timeout=0)

        Git.AutoInterrupt._status_code_if_terminate = default
