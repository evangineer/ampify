#! /usr/bin/env python

# Changes to this file by The Ampify Authors are according to the
# Public Domain license that can be found in the root LICENSE file.

# This file was adapted from depot_tools/presubmit_support.py in the Chromium
# repository and has the following License:

# Copyright (c) 2006-2009 The Chromium Authors. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
#    * Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#    * Redistributions in binary form must reproduce the above
# copyright notice, this list of conditions and the following disclaimer
# in the documentation and/or other materials provided with the
# distribution.
#    * Neither the name of Google Inc. nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""Enables directory-specific review scripts within in a code repository."""

__version__ = '1.3.4'

# TODO(joi) Add caching where appropriate/needed. The API is designed to allow
# caching (between all different invocations of review scripts for a given
# change). We should add it as our review scripts start feeling slow.

import cPickle  # Exposed through the API.
import cStringIO  # Exposed through the API.
import exceptions
import fnmatch
import glob
import logging
import marshal  # Exposed through the API.
import optparse
import os  # Somewhat exposed through the API.
import pickle  # Exposed through the API.
import random
import re  # Exposed through the API.
import subprocess  # Exposed through the API.
import sys  # Parts exposed through API.
import tempfile  # Exposed through the API.
import time
import traceback  # Exposed through the API.
import types
import unittest  # Exposed through the API.
import urllib2  # Exposed through the API.
import warnings

# Local imports.
import reviewbuiltins

from pyutil import scm
from pyutil.env import read_file


class NotImplementedException(Exception):
  """We're leaving placeholders in a bunch of places to remind us of the
  design of the API, but we have not implemented all of it yet. Implement as
  the need arises.
  """
  pass


def normpath(path):
  '''Version of os.path.normpath that also changes backward slashes to
  forward slashes when not running on Windows.
  '''
  # This is safe to always do because the Windows version of os.path.normpath
  # will replace forward slashes with backward slashes.
  path = path.replace(os.sep, '/')
  return os.path.normpath(path)

def PromptYesNo(input_stream, output_stream, prompt):
  output_stream.write(prompt)
  response = input_stream.readline().strip().lower()
  return response == 'y' or response == 'yes'

class OutputApi(object):
  """This class (more like a module) gets passed to review scripts so that
  they can specify various types of results.
  """

  class ReviewResult(object):
    """Base class for result objects."""

    def __init__(self, message, items=None, long_text=''):
      """
      message: A short one-line message to indicate errors.
      items: A list of short strings to indicate where errors occurred.
      long_text: multi-line text output, e.g. from another tool
      """
      self._message = message
      self._items = []
      if items:
        self._items = items
      self._long_text = long_text.rstrip()

    def _Handle(self, output_stream, input_stream, may_prompt=True):
      """Writes this result to the output stream.

      Args:
        output_stream: Where to write

      Returns:
        True if execution may continue, False otherwise.
      """
      output_stream.write(self._message)
      output_stream.write('\n')
      for item in self._items:
        output_stream.write('  %s\n' % str(item))
      if self._long_text:
        output_stream.write('\n***************\n%s\n***************\n' %
                            self._long_text)

      if self.ShouldPrompt() and may_prompt:
        if not PromptYesNo(input_stream, output_stream,
                           'Are you sure you want to continue? (y/N): '):
          return False

      return not self.IsFatal()

    def IsFatal(self):
      """An error that is fatal stops g4 mail/submit immediately, i.e. before
      other review scripts are run.
      """
      return False

    def ShouldPrompt(self):
      """Whether this review result should result in a prompt warning."""
      return False

  class ReviewError(ReviewResult):
    """A hard review error."""
    def IsFatal(self):
      return True

  class ReviewPromptWarning(ReviewResult):
    """An warning that prompts the user if they want to continue."""
    def ShouldPrompt(self):
      return True

  class ReviewNotifyResult(ReviewResult):
    """Just print something to the screen -- but it's not even a warning."""
    pass

  class MailTextResult(ReviewResult):
    """A warning that should be included in the review request email."""
    def __init__(self, *args, **kwargs):
      raise NotImplementedException()  # TODO(joi) Implement.


class InputApi(object):
  """An instance of this object is passed to review scripts so they can
  know stuff about the change they're looking at.
  """

  # File extensions that are considered source files from a style guide
  # perspective. Don't modify this list from a review script!
  DEFAULT_WHITE_LIST = (
      # C++ and friends
      r".*\.c", r".*\.cc", r".*\.cpp", r".*\.h", r".*\.m", r".*\.mm",
      r".*\.inl", r".*\.asm", r".*\.hxx", r".*\.hpp", r".*\.go",
      # Scripts
      r".*\.js", r".*\.py", r".*\.json", r".*\.sh", r".*\.rb",
      # No extension at all
      r"(^|.*[\\\/])[^.]+$",
      # Other
      r".*\.java", r".*\.mk", r".*\.am", r".*\.txt", r".*\.md",
  )

  # Path regexp that should be excluded from being considered containing source
  # files. Don't modify this list from a review script!
  DEFAULT_BLACK_LIST = (
      r".*\bexperimental[\\\/].*",
      r".*\bthird_party[\\\/].*",
      # Output directories (just in case)
      r".*\bDebug[\\\/].*",
      r".*\bRelease[\\\/].*",
      r".*\bxcodebuild[\\\/].*",
      r".*\bsconsbuild[\\\/].*",
      # All caps files like README and LICENCE.
      r".*\b[A-Z0-9_]+$",
      # SCM (can happen in dual SCM configuration). (Slightly over aggressive)
      r".*\.git[\\\/].*",
      r".*\.svn[\\\/].*",
  )

  def __init__(self, change, review_path, is_committing):
    """Builds an InputApi object.

    Args:
      change: A review.Change object.
      review_path: The path to the review script being processed.
      is_committing: True if the change is about to be committed.
    """
    self.version = [int(x) for x in __version__.split('.')]
    self.change = change
    self.is_committing = is_committing

    # We expose various modules and functions as attributes of the input_api
    # so that review scripts don't have to import them.
    self.basename = os.path.basename
    self.cPickle = cPickle
    self.cStringIO = cStringIO
    self.os_path = os.path
    self.pickle = pickle
    self.marshal = marshal
    self.re = re
    self.subprocess = subprocess
    self.tempfile = tempfile
    self.traceback = traceback
    self.unittest = unittest
    self.urllib2 = urllib2

    # To easily fork python.
    self.python_executable = sys.executable
    self.environ = os.environ

    # InputApi.platform is the platform you're currently running on.
    self.platform = sys.platform

    # The local path of the currently-being-processed review script.
    self._current_review_path = os.path.dirname(review_path)

    # We carry the builtin checks so review scripts can easily use them.
    self.builtins = reviewbuiltins

  def ReviewLocalPath(self):
    """Returns the local path of the review script currently being run.

    This is useful if you don't want to hard-code absolute paths in the
    review script.  For example, It can be used to find another file
    relative to the review script, so the whole tree can be branched and
    the review script still works, without editing its content.
    """
    return self._current_review_path

  def AffectedFiles(self, include_dirs=False, include_deletes=True):
    """Same as input_api.change.AffectedFiles() except only lists files
    (and optionally directories) in the same directory as the current review
    script, or subdirectories thereof.
    """
    dir_with_slash = normpath("%s/" % self.ReviewLocalPath())
    if len(dir_with_slash) == 1:
      dir_with_slash = ''
    return filter(
        lambda x: normpath(x.AbsoluteLocalPath()).startswith(dir_with_slash),
        self.change.AffectedFiles(include_dirs, include_deletes))

  def LocalPaths(self, include_dirs=False):
    """Returns local paths of input_api.AffectedFiles()."""
    return [af.LocalPath() for af in self.AffectedFiles(include_dirs)]

  def AbsoluteLocalPaths(self, include_dirs=False):
    """Returns absolute local paths of input_api.AffectedFiles()."""
    return [af.AbsoluteLocalPath() for af in self.AffectedFiles(include_dirs)]

  def ServerPaths(self, include_dirs=False):
    """Returns server paths of input_api.AffectedFiles()."""
    return [af.ServerPath() for af in self.AffectedFiles(include_dirs)]

  def AffectedTextFiles(self, include_deletes=None):
    """Same as input_api.change.AffectedTextFiles() except only lists files
    in the same directory as the current review script, or subdirectories
    thereof.
    """
    if include_deletes is not None:
      warnings.warn("AffectedTextFiles(include_deletes=%s)"
                        " is deprecated and ignored" % str(include_deletes),
                    category=DeprecationWarning,
                    stacklevel=2)
    return filter(lambda x: x.IsTextFile(),
                  self.AffectedFiles(include_dirs=False, include_deletes=False))

  def FilterSourceFile(self, affected_file, white_list=None, black_list=None):
    """Filters out files that aren't considered "source file".

    If white_list or black_list is None, InputApi.DEFAULT_WHITE_LIST
    and InputApi.DEFAULT_BLACK_LIST is used respectively.

    The lists will be compiled as regular expression and
    AffectedFile.LocalPath() needs to pass both list.

    Note: Copy-paste this function to suit your needs or use a lambda function.
    """
    def Find(affected_file, list):
      for item in list:
        local_path = affected_file.LocalPath()
        if self.re.match(item, local_path):
          logging.debug("%s matched %s" % (item, local_path))
          return True
      return False
    return (Find(affected_file, white_list or self.DEFAULT_WHITE_LIST) and
            not Find(affected_file, black_list or self.DEFAULT_BLACK_LIST))

  def AffectedSourceFiles(self, source_file):
    """Filter the list of AffectedTextFiles by the function source_file.

    If source_file is None, InputApi.FilterSourceFile() is used.
    """
    if not source_file:
      source_file = self.FilterSourceFile
    return filter(source_file, self.AffectedTextFiles())

  def RightHandSideLines(self, source_file_filter=None):
    """An iterator over all text lines in "new" version of changed files.

    Only lists lines from new or modified text files in the change that are
    contained by the directory of the currently executing review script.

    This is useful for doing line-by-line regex checks, like checking for
    trailing whitespace.

    Yields:
      a 3 tuple:
        the AffectedFile instance of the current file;
        integer line number (1-based); and
        the contents of the line as a string.

    Note: The cariage return (LF or CR) is stripped off.
    """
    files = self.AffectedSourceFiles(source_file_filter)
    return InputApi._RightHandSideLinesImpl(files)

  def ReadFile(self, file_item, mode='r'):
    """Reads an arbitrary file.

    Deny reading anything outside the repository.
    """
    if isinstance(file_item, AffectedFile):
      file_item = file_item.AbsoluteLocalPath()
    if not file_item.startswith(self.change.RepositoryRoot()):
      raise IOError('Access outside the repository root is denied.')
    return read_file(file_item, mode)

  @staticmethod
  def _RightHandSideLinesImpl(affected_files):
    """Implements RightHandSideLines for InputApi and GclChange."""
    for af in affected_files:
      lines = af.NewContents()
      line_number = 0
      for line in lines:
        line_number += 1
        yield (af, line_number, line)


class AffectedFile(object):
  """Representation of a file in a change."""

  def __init__(self, path, action, repository_root=''):
    self._path = path
    self._action = action
    self._local_root = repository_root
    self._is_directory = None
    self._properties = {}

  def ServerPath(self):
    """Returns a path string that identifies the file in the SCM system.

    Returns the empty string if the file does not exist in SCM.
    """
    return ""

  def LocalPath(self):
    """Returns the path of this file on the local disk relative to client root.
    """
    return normpath(self._path)

  def AbsoluteLocalPath(self):
    """Returns the absolute path of this file on the local disk.
    """
    return os.path.abspath(os.path.join(self._local_root, self.LocalPath()))

  def IsDirectory(self):
    """Returns true if this object is a directory."""
    if self._is_directory is None:
      path = self.AbsoluteLocalPath()
      self._is_directory = (os.path.exists(path) and
                            os.path.isdir(path))
    return self._is_directory

  def Action(self):
    """Returns the action on this opened file, e.g. A, M, D, etc."""
    # TODO(maruel): Somewhat crappy, Could be "A" or "A  +" for svn but
    # different for other SCM.
    return self._action

  def Property(self, property_name):
    """Returns the specified SCM property of this file, or None if no such
    property.
    """
    return self._properties.get(property_name, None)

  def IsTextFile(self):
    """Returns True if the file is a text file and not a binary file.

    Deleted files are not text file."""
    raise NotImplementedError()  # Implement when needed

  def NewContents(self):
    """Returns an iterator over the lines in the new version of file.

    The new version is the file in the user's workspace, i.e. the "right hand
    side".

    Contents will be empty if the file is a directory or does not exist.
    Note: The cariage returns (LF or CR) are stripped off.
    """
    if self.IsDirectory():
      return []
    else:
      return read_file(self.AbsoluteLocalPath(),
                                    'rU').splitlines()

  def OldContents(self):
    """Returns an iterator over the lines in the old version of file.

    The old version is the file in depot, i.e. the "left hand side".
    """
    raise NotImplementedError()  # Implement when needed

  def OldFileTempPath(self):
    """Returns the path on local disk where the old contents resides.

    The old version is the file in depot, i.e. the "left hand side".
    This is a read-only cached copy of the old contents. *DO NOT* try to
    modify this file.
    """
    raise NotImplementedError()  # Implement if/when needed.

  def __str__(self):
    return self.LocalPath()


class GitAffectedFile(AffectedFile):
  """Representation of a file in a change out of a git checkout."""

  def __init__(self, *args, **kwargs):
    AffectedFile.__init__(self, *args, **kwargs)
    self._server_path = None
    self._is_text_file = None

  def ServerPath(self):
    if self._server_path is None:
      raise NotImplementedException()  # TODO(maruel) Implement.
    return self._server_path

  def IsDirectory(self):
    if self._is_directory is None:
      path = self.AbsoluteLocalPath()
      if os.path.exists(path):
        # Retrieve directly from the file system; it is much faster than
        # querying subversion, especially on Windows.
        self._is_directory = os.path.isdir(path)
      else:
        # raise NotImplementedException()  # TODO(maruel) Implement.
        self._is_directory = False
    return self._is_directory

  def Property(self, property_name):
    if not property_name in self._properties:
      raise NotImplementedException()  # TODO(maruel) Implement.
    return self._properties[property_name]

  def IsTextFile(self):
    if self._is_text_file is None:
      if self.Action() == 'D':
        # A deleted file is not a text file.
        self._is_text_file = False
      elif self.IsDirectory():
        self._is_text_file = False
      else:
        # raise NotImplementedException()  # TODO(maruel) Implement.
        self._is_text_file = os.path.isfile(self.AbsoluteLocalPath())
    return self._is_text_file


class Change(object):
  """Describe a change.

  Used directly by the review scripts to query the current change being
  tested.

  Instance members:
    tags: Dictionnary of KEY=VALUE pairs found in the change description.
    self.KEY: equivalent to tags['KEY']
  """

  _AFFECTED_FILES = AffectedFile

  # Matches key/value (or "tag") lines in changelist descriptions.
  _TAG_LINE_RE = re.compile(
      '^\s*(?P<key>[A-Z][A-Z_0-9]*)\s*=\s*(?P<value>.*?)\s*$')

  def __init__(self, name, description, local_root, files, issue, patchset):
    if files is None:
      files = []
    self._name = name
    self._full_description = description
    # Convert root into an absolute path.
    self._local_root = os.path.abspath(local_root)
    self.issue = issue
    self.patchset = patchset
    self.scm = ''

    # From the description text, build up a dictionary of key/value pairs
    # plus the description minus all key/value or "tag" lines.
    self._description_without_tags = []
    self.tags = {}
    for line in self._full_description.splitlines():
      m = self._TAG_LINE_RE.match(line)
      if m:
        self.tags[m.group('key')] = m.group('value')
      else:
        self._description_without_tags.append(line)

    # Change back to text and remove whitespace at end.
    self._description_without_tags = '\n'.join(self._description_without_tags)
    self._description_without_tags = self._description_without_tags.rstrip()

    self._affected_files = [
        self._AFFECTED_FILES(info[1], info[0].strip(), self._local_root)
        for info in files
    ]

  def Name(self):
    """Returns the change name."""
    return self._name

  def DescriptionText(self):
    """Returns the user-entered changelist description, minus tags.

    Any line in the user-provided description starting with e.g. "FOO="
    (whitespace permitted before and around) is considered a tag line.  Such
    lines are stripped out of the description this function returns.
    """
    return self._description_without_tags

  def FullDescriptionText(self):
    """Returns the complete changelist description including tags."""
    return self._full_description

  def RepositoryRoot(self):
    """Returns the repository (checkout) root directory for this change,
    as an absolute path.
    """
    return self._local_root

  def __getattr__(self, attr):
    """Return tags directly as attributes on the object."""
    if not re.match(r"^[A-Z_]*$", attr):
      raise AttributeError(self, attr)
    return self.tags.get(attr)

  def AffectedFiles(self, include_dirs=False, include_deletes=True):
    """Returns a list of AffectedFile instances for all files in the change.

    Args:
      include_deletes: If false, deleted files will be filtered out.
      include_dirs: True to include directories in the list

    Returns:
      [AffectedFile(path, action), AffectedFile(path, action)]
    """
    if include_dirs:
      affected = self._affected_files
    else:
      affected = filter(lambda x: not x.IsDirectory(), self._affected_files)

    if include_deletes:
      return affected
    else:
      return filter(lambda x: x.Action() != 'D', affected)

  def AffectedTextFiles(self, include_deletes=None):
    """Return a list of the existing text files in a change."""
    if include_deletes is not None:
      warnings.warn("AffectedTextFiles(include_deletes=%s)"
                        " is deprecated and ignored" % str(include_deletes),
                    category=DeprecationWarning,
                    stacklevel=2)
    return filter(lambda x: x.IsTextFile(),
                  self.AffectedFiles(include_dirs=False, include_deletes=False))

  def LocalPaths(self, include_dirs=False):
    """Convenience function."""
    return [af.LocalPath() for af in self.AffectedFiles(include_dirs)]

  def AbsoluteLocalPaths(self, include_dirs=False):
    """Convenience function."""
    return [af.AbsoluteLocalPath() for af in self.AffectedFiles(include_dirs)]

  def ServerPaths(self, include_dirs=False):
    """Convenience function."""
    return [af.ServerPath() for af in self.AffectedFiles(include_dirs)]

  def RightHandSideLines(self):
    """An iterator over all text lines in "new" version of changed files.

    Lists lines from new or modified text files in the change.

    This is useful for doing line-by-line regex checks, like checking for
    trailing whitespace.

    Yields:
      a 3 tuple:
        the AffectedFile instance of the current file;
        integer line number (1-based); and
        the contents of the line as a string.
    """
    return InputApi._RightHandSideLinesImpl(
        filter(lambda x: x.IsTextFile(),
               self.AffectedFiles(include_deletes=False)))


class GitChange(Change):
  _AFFECTED_FILES = GitAffectedFile

  def __init__(self, *args, **kwargs):
    Change.__init__(self, *args, **kwargs)
    self.scm = 'git'


def ListRelevantReviewFiles(files, root):
  """Finds all review files that apply to a given set of source files.

  Args:
    files: An iterable container containing file paths.
    root: Path where to stop searching.

  Return:
    List of absolute paths of the existing review scripts.
  """
  entries = []
  for f in files:
    f = normpath(os.path.join(root, f))
    while f:
      f = os.path.dirname(f)
      if f in entries:
        break
      entries.append(f)
      if f == root:
        break
  entries.sort()
  entries = map(lambda x: os.path.join(x, '.review.py'), entries)
  return filter(lambda x: os.path.isfile(x), entries)


class GetTrySlavesExecuter(object):
  def ExecReviewScript(self, script_text):
    """Executes GetPreferredTrySlaves() from a single review script.

    Args:
      script_text: The text of the review script.

    Return:
      A list of try slaves.
    """
    context = {}
    exec script_text in context

    function_name = 'GetPreferredTrySlaves'
    if function_name in context:
      result = eval(function_name + '()', context)
      if not isinstance(result, types.ListType):
        raise exceptions.RuntimeError(
            'Review functions must return a list, got a %s instead: %s' %
            (type(result), str(result)))
      for item in result:
        if not isinstance(item, basestring):
          raise exceptions.RuntimeError('All try slaves names must be strings.')
        if item != item.strip():
          raise exceptions.RuntimeError('Try slave names cannot start/end'
                                        'with whitespace')
    else:
      result = []
    return result


def DoGetTrySlaves(changed_files,
                   repository_root,
                   default_review,
                   verbose,
                   output_stream):
  """Get the list of try servers from the review scripts.

  Args:
    changed_files: List of modified files.
    repository_root: The repository root.
    default_review: A default review script to execute in any case.
    verbose: Prints debug info.
    output_stream: A stream to write debug output to.

  Return:
    List of try slaves
  """
  review_files = ListRelevantReviewFiles(changed_files, repository_root)
  if not review_files and verbose:
    output_stream.write("Warning, no review script found.\n")
  results = []
  executer = GetTrySlavesExecuter()
  if default_review:
    if verbose:
      output_stream.write("Running default review script.\n")
    results += executer.ExecReviewScript(default_review)
  for filename in review_files:
    filename = os.path.abspath(filename)
    if verbose:
      output_stream.write("Running %s\n" % filename)
    # Accept CRLF review script.
    review_script = read_file(filename, 'rU')
    results += executer.ExecReviewScript(review_script)

  slaves = list(set(results))
  if slaves and verbose:
    output_stream.write(', '.join(slaves))
    output_stream.write('\n')
  return slaves


class ReviewExecuter(object):
  def __init__(self, change, committing):
    """
    Args:
      change: The Change object.
      committing: True if 'gcl commit' is running, False if 'gcl upload' is.
    """
    self.change = change
    self.committing = committing

  def ExecReviewScript(self, script_text, review_path):
    """Executes a single review script.

    Args:
      script_text: The text of the review script.
      review_path: The path to the review file (this will be reported via
        input_api.ReviewLocalPath()).

    Return:
      A list of result objects, empty if no problems.
    """

    # Change to the review file's directory to support local imports.
    main_path = os.getcwd()
    os.chdir(os.path.dirname(review_path))

    # Load the review script into context.
    input_api = InputApi(self.change, review_path, self.committing)
    context = {}
    exec script_text in context

    # These function names must change if we make substantial changes to
    # the review API that are not backwards compatible.
    if self.committing:
      function_name = 'CheckChangeOnCommit'
    else:
      function_name = 'CheckChangeOnUpload'
    if function_name in context:
      context['__args'] = (input_api, OutputApi())
      result = eval(function_name + '(*__args)', context)
      if not (isinstance(result, types.TupleType) or
              isinstance(result, types.ListType)):
        raise exceptions.RuntimeError(
          'Review functions must return a tuple or list')
      for item in result:
        if not isinstance(item, OutputApi.ReviewResult):
          raise exceptions.RuntimeError(
            'All review results must be of types derived from '
            'output_api.ReviewResult')
    else:
      result = ()  # no error since the script doesn't care about current event.

    # Return the process to the original working directory.
    os.chdir(main_path)
    return result


def DoReviewChecks(change,
                      committing,
                      verbose,
                      output_stream,
                      input_stream,
                      default_review,
                      may_prompt):
  """Runs all review checks that apply to the files in the change.

  This finds all .review.py files in directories enclosing the files in the
  change (up to the repository root) and calls the relevant entrypoint function
  depending on whether the change is being committed or uploaded.

  Prints errors, warnings and notifications.  Prompts the user for warnings
  when needed.

  Args:
    change: The Change object.
    committing: True if 'gcl commit' is running, False if 'gcl upload' is.
    verbose: Prints debug info.
    output_stream: A stream to write output from review tests to.
    input_stream: A stream to read input from the user.
    default_review: A default review script to execute in any case.
    may_prompt: Enable (y/n) questions on warning or error.

  Warning:
    If may_prompt is true, output_stream SHOULD be sys.stdout and input_stream
    SHOULD be sys.stdin.

  Return:
    True if execution can continue, False if not.
  """
  start_time = time.time()
  review_files = ListRelevantReviewFiles(change.AbsoluteLocalPaths(True),
                                               change.RepositoryRoot())
  if not review_files and verbose:
    output_stream.write("Warning, no .review.py found.\n")
  results = []
  executer = ReviewExecuter(change, committing)
  if default_review:
    if verbose:
      output_stream.write("Running default review script.\n")
    fake_path = os.path.join(change.RepositoryRoot(), '.review.py')
    results += executer.ExecReviewScript(default_review, fake_path)
  for filename in review_files:
    filename = os.path.abspath(filename)
    if verbose:
      output_stream.write("Running %s\n" % filename)
    # Accept CRLF review script.
    review_script = read_file(filename, 'rU')
    results += executer.ExecReviewScript(review_script, filename)

  errors = []
  notifications = []
  warnings = []
  for result in results:
    if not result.IsFatal() and not result.ShouldPrompt():
      notifications.append(result)
    elif result.ShouldPrompt():
      warnings.append(result)
    else:
      errors.append(result)

  error_count = 0
  for name, items in (('Messages', notifications),
                      ('Warnings', warnings),
                      ('ERRORS', errors)):
    if items:
      output_stream.write('** Review %s **\n' % name)
      for item in items:
        if not item._Handle(output_stream, input_stream,
                            may_prompt=False):
          error_count += 1
        output_stream.write('\n')

  total_time = time.time() - start_time
  if total_time > 1.0:
    print "Review checks took %.1fs to calculate." % total_time

  if not errors and warnings and may_prompt:
    if not PromptYesNo(input_stream, output_stream,
                       'There were review warnings. '
                       'Are you sure you wish to continue? (y/N): '):
      error_count += 1

  return (error_count == 0)


def ScanSubDirs(mask, recursive):
  if not recursive:
    return [x for x in glob.glob(mask) if '.svn' not in x and '.git' not in x]
  else:
    results = []
    for root, dirs, files in os.walk('.'):
      if '.svn' in dirs:
        dirs.remove('.svn')
      if '.git' in dirs:
        dirs.remove('.git')
      for name in files:
        if fnmatch.fnmatch(name, mask):
          results.append(os.path.join(root, name))
    return results


def ParseFiles(args, recursive):
  files = []
  for arg in args:
    files.extend([('M', f) for f in ScanSubDirs(arg, recursive)])
  return files


def Main(argv):
  parser = optparse.OptionParser(usage="%prog [options]",
                                 version="%prog " + str(__version__))
  parser.add_option("-c", "--commit", action="store_true", default=False,
                   help="Use commit instead of upload checks")
  parser.add_option("-u", "--upload", action="store_false", dest='commit',
                   help="Use upload instead of commit checks")
  parser.add_option("-r", "--recursive", action="store_true",
                   help="Act recursively")
  parser.add_option("-v", "--verbose", action="store_true", default=False,
                   help="Verbose output")
  parser.add_option("--files")
  parser.add_option("--name", default='no name')
  parser.add_option("--description", default='')
  parser.add_option("--issue", type='int', default=0)
  parser.add_option("--patchset", type='int', default=0)
  parser.add_option("--root", default='')
  parser.add_option("--default_review")
  parser.add_option("--may_prompt", action='store_true', default=False)
  options, args = parser.parse_args(argv[1:])
  if not options.root:
    options.root = os.getcwd()
  if os.path.isdir(os.path.join(options.root, '.git')):
    change_class = GitChange
    if not options.files:
      if args:
        options.files = ParseFiles(args, options.recursive)
      else:
        # Grab modified files.
        options.files = scm.GIT.CaptureStatus([options.root])
  else:
    # Doesn't seem under source control.
    change_class = Change
  if options.verbose:
    if len(options.files) != 1:
      print "Found %d files." % len(options.files)
    else:
      print "Found 1 file."
  return not DoReviewChecks(change_class(options.name,
                                         options.description,
                                         options.root,
                                         options.files,
                                         options.issue,
                                         options.patchset),
                            options.commit,
                            options.verbose,
                            sys.stdout,
                            sys.stdin,
                            options.default_review,
                            options.may_prompt)


if __name__ == '__main__':
  sys.exit(Main(sys.argv))
