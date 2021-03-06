# -*- coding: utf-8 -*-
# Copyright 2014 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from twisted.internet import defer

from synapse.federation.pdu_codec import encode_event_id, decode_event_id
from synapse.util.logutils import log_function

from collections import namedtuple

import logging
import hashlib

logger = logging.getLogger(__name__)


def _get_state_key_from_event(event):
    return event.state_key


KeyStateTuple = namedtuple("KeyStateTuple", ("context", "type", "state_key"))


class StateHandler(object):
    """ Repsonsible for doing state conflict resolution.
    """

    def __init__(self, hs):
        self.store = hs.get_datastore()
        self._replication = hs.get_replication_layer()
        self.server_name = hs.hostname

    @defer.inlineCallbacks
    @log_function
    def handle_new_event(self, event, snapshot):
        """ Given an event this works out if a) we have sufficient power level
        to update the state and b) works out what the prev_state should be.

        Returns:
            Deferred: Resolved with a boolean indicating if we succesfully
            updated the state.

        Raised:
            AuthError
        """
        # This needs to be done in a transaction.

        if not hasattr(event, "state_key"):
            return

        key = KeyStateTuple(
            event.room_id,
            event.type,
            _get_state_key_from_event(event)
        )

        # Now I need to fill out the prev state and work out if it has auth
        # (w.r.t. to power levels)

        snapshot.fill_out_prev_events(event)

        event.prev_events = [
            e for e in event.prev_events if e != event.event_id
        ]

        current_state = snapshot.prev_state_pdu

        if current_state:
            event.prev_state = encode_event_id(
                current_state.pdu_id, current_state.origin
            )

        # TODO check current_state to see if the min power level is less
        # than the power level of the user
        # power_level = self._get_power_level_for_event(event)

        pdu_id, origin = decode_event_id(event.event_id, self.server_name)

        yield self.store.update_current_state(
            pdu_id=pdu_id,
            origin=origin,
            context=key.context,
            pdu_type=key.type,
            state_key=key.state_key
        )

        defer.returnValue(True)

    @defer.inlineCallbacks
    @log_function
    def handle_new_state(self, new_pdu):
        """ Apply conflict resolution to `new_pdu`.

        This should be called on every new state pdu, regardless of whether or
        not there is a conflict.

        This function is safe against the race of it getting called with two
        `PDU`s trying to update the same state.
        """

        # This needs to be done in a transaction.

        is_new = yield self._handle_new_state(new_pdu)

        logger.debug("is_new: %s %s %s", is_new, new_pdu.pdu_id, new_pdu.origin)

        if is_new:
            yield self.store.update_current_state(
                pdu_id=new_pdu.pdu_id,
                origin=new_pdu.origin,
                context=new_pdu.context,
                pdu_type=new_pdu.pdu_type,
                state_key=new_pdu.state_key
            )

        defer.returnValue(is_new)

    def _get_power_level_for_event(self, event):
        # return self._persistence.get_power_level_for_user(event.room_id,
            # event.sender)
        return event.power_level

    @defer.inlineCallbacks
    @log_function
    def _handle_new_state(self, new_pdu):
        tree, missing_branch = yield self.store.get_unresolved_state_tree(
            new_pdu
        )
        new_branch, current_branch = tree

        logger.debug(
            "_handle_new_state new=%s, current=%s",
            new_branch, current_branch
        )

        if missing_branch is not None:
            # We're missing some PDUs. Fetch them.
            # TODO (erikj): Limit this.
            missing_prev = tree[missing_branch][-1]

            pdu_id = missing_prev.prev_state_id
            origin = missing_prev.prev_state_origin

            is_missing = yield self.store.get_pdu(pdu_id, origin) is None
            if not is_missing:
                raise Exception("Conflict resolution failed")

            yield self._replication.get_pdu(
                destination=missing_prev.origin,
                pdu_origin=origin,
                pdu_id=pdu_id,
                outlier=True
            )

            updated_current = yield self._handle_new_state(new_pdu)
            defer.returnValue(updated_current)

        if not current_branch:
            # There is no current state
            defer.returnValue(True)
            return

        n = new_branch[-1]
        c = current_branch[-1]

        common_ancestor = n.pdu_id == c.pdu_id and n.origin == c.origin

        if common_ancestor:
            # We found a common ancestor!

            if len(current_branch) == 1:
                # This is a direct clobber so we can just...
                defer.returnValue(True)

        else:
            # We didn't find a common ancestor. This is probably fine.
            pass

        result = yield self._do_conflict_res(
            new_branch, current_branch, common_ancestor
        )
        defer.returnValue(result)

    @defer.inlineCallbacks
    def _do_conflict_res(self, new_branch, current_branch, common_ancestor):
        conflict_res = [
            self._do_power_level_conflict_res,
            self._do_chain_length_conflict_res,
            self._do_hash_conflict_res,
        ]

        for algo in conflict_res:
            new_res, curr_res = yield defer.maybeDeferred(
                algo,
                new_branch, current_branch, common_ancestor
            )

            if new_res < curr_res:
                defer.returnValue(False)
            elif new_res > curr_res:
                defer.returnValue(True)

        raise Exception("Conflict resolution failed.")

    @defer.inlineCallbacks
    def _do_power_level_conflict_res(self, new_branch, current_branch,
                                     common_ancestor):
        new_powers_deferreds = []
        for e in new_branch[:-1] if common_ancestor else new_branch:
            if hasattr(e, "user_id"):
                new_powers_deferreds.append(
                    self.store.get_power_level(e.context, e.user_id)
                )

        current_powers_deferreds = []
        for e in current_branch[:-1] if common_ancestor else current_branch:
            if hasattr(e, "user_id"):
                current_powers_deferreds.append(
                    self.store.get_power_level(e.context, e.user_id)
                )

        new_powers = yield defer.gatherResults(
            new_powers_deferreds,
            consumeErrors=True
        )

        current_powers = yield defer.gatherResults(
            current_powers_deferreds,
            consumeErrors=True
        )

        max_power_new = max(new_powers)
        max_power_current = max(current_powers)

        defer.returnValue(
            (max_power_new, max_power_current)
        )

    def _do_chain_length_conflict_res(self, new_branch, current_branch,
                                      common_ancestor):
        return (len(new_branch), len(current_branch))

    def _do_hash_conflict_res(self, new_branch, current_branch,
                              common_ancestor):
        new_str = "".join([p.pdu_id + p.origin for p in new_branch])
        c_str = "".join([p.pdu_id + p.origin for p in current_branch])

        return (
            hashlib.sha1(new_str).hexdigest(),
            hashlib.sha1(c_str).hexdigest()
        )
