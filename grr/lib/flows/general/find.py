#!/usr/bin/env python
# Copyright 2012 Google Inc. All Rights Reserved.
"""Find files on the client."""
import stat

from grr.client.client_actions import searching as searching_actions
from grr.lib import aff4
from grr.lib import flow
from grr.lib.aff4_objects import aff4_grr
from grr.lib.aff4_objects import standard
from grr.lib.rdfvalues import structs as rdf_structs
from grr.proto import flows_pb2


class FindFilesArgs(rdf_structs.RDFProtoStruct):
  protobuf = flows_pb2.FindFilesArgs

  def Validate(self):
    """Ensure that the request is sane."""
    self.findspec.Validate()


class FindFiles(flow.GRRFlow):
  r"""Find files on the client.

    The logic is:
    - Find files under "Path"
    - Filter for files with os.path.basename matching "Path Regular Expression"
    - Filter for files with sizes between min and max limits
    - Filter for files that contain "Data Regular Expression" in the first 1MB
        of file data
    - Return a StatEntry rdfvalue for each of the results

    Path and data regexes, and file size limits are optional. Don"t encode path
    information in the regex.  See correct usage below.

    Example:

    Path="/usr/local"
    Path Regular Expression="admin"

    Match: "/usr/local/bin/admin"      (file)
    Match: "/usr/local/admin"          (directory)
    No Match: "/usr/admin/local/blah"

    The result from this flow is a list of StatEntry objects, one for
    each file matching the criteria. Matching files will not be
    downloaded by this flow, only the metadata of the file is fetched.

    Note: This flow is inefficient for collecting a large number of files.

  Returns to parent flow:
    rdf_client.StatEntry objects for each found file.
  """

  category = "/Filesystem/"
  args_type = FindFilesArgs
  friendly_name = "Find Files"

  @flow.StateHandler()
  def Start(self, unused_response):
    """Issue the find request to the client."""
    self.state.received_count = 0

    # Build up the request protobuf.
    self.args.findspec.iterator.number = self.args.iteration_count

    # Convert the filename glob to a regular expression.
    if self.args.findspec.path_glob:
      self.args.findspec.path_regex = self.args.findspec.path_glob.AsRegEx()

    # Call the client with it
    self.CallClient(
        searching_actions.Find, self.args.findspec, next_state="IterateFind")

  @flow.StateHandler()
  def IterateFind(self, responses):
    """Iterate in this state until no more results are available."""
    if not responses.success:
      raise IOError(responses.status)

    for response in responses:
      # Create the file in the VFS
      vfs_urn = response.hit.pathspec.AFF4Path(self.client_id)

      # TODO(user): This ends up being fairly expensive.
      if stat.S_ISDIR(response.hit.st_mode):
        fd = aff4.FACTORY.Create(
            vfs_urn, standard.VFSDirectory, token=self.token)
      else:
        fd = aff4.FACTORY.Create(vfs_urn, aff4_grr.VFSFile, token=self.token)

      stat_response = fd.Schema.STAT(response.hit)
      fd.Set(stat_response)

      fd.Set(fd.Schema.PATHSPEC(response.hit.pathspec))
      fd.Close(sync=False)

      # Send the stat to the parent flow.
      self.SendReply(stat_response)

    self.state.received_count += len(responses)

    # Exit if we hit the max result count we wanted or we're finished. Note that
    # we may exceed the max_results if the iteration yielded too many results,
    # we simply will not return to the client for another iteration.
    if (self.state.received_count < self.args.max_results and
        responses.iterator.state != responses.iterator.State.FINISHED):

      self.args.findspec.iterator = responses.iterator

      # If we are close to max_results reduce the iterator.
      self.args.findspec.iterator.number = min(
          self.args.findspec.iterator.number,
          self.args.max_results - self.state.received_count)

      self.CallClient(
          searching_actions.Find, self.args.findspec, next_state="IterateFind")
      self.Log("%d files processed.", self.state.received_count)
