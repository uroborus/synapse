/*
Copyright 2014 OpenMarket Ltd

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

'use strict';

var forAllVideoTracksOnStream = function(s, f) {
    var tracks = s.getVideoTracks();
    for (var i = 0; i < tracks.length; i++) {
        f(tracks[i]);
    }
}

var forAllAudioTracksOnStream = function(s, f) {
    var tracks = s.getAudioTracks();
    for (var i = 0; i < tracks.length; i++) {
        f(tracks[i]);
    }
}

var forAllTracksOnStream = function(s, f) {
    forAllVideoTracksOnStream(s, f);
    forAllAudioTracksOnStream(s, f);
}

navigator.getUserMedia = navigator.getUserMedia || navigator.webkitGetUserMedia || navigator.mozGetUserMedia;
window.RTCPeerConnection = window.RTCPeerConnection || window.webkitRTCPeerConnection; // but not mozRTCPeerConnection because its interface is not compatible
window.RTCSessionDescription = window.RTCSessionDescription || window.webkitRTCSessionDescription || window.mozRTCSessionDescription;
window.RTCIceCandidate = window.RTCIceCandidate || window.webkitRTCIceCandidate || window.mozRTCIceCandidate;

angular.module('MatrixCall', [])
.factory('MatrixCall', ['matrixService', 'matrixPhoneService', '$rootScope', '$timeout', function MatrixCallFactory(matrixService, matrixPhoneService, $rootScope, $timeout) {
    var MatrixCall = function(room_id) {
        this.room_id = room_id;
        this.call_id = "c" + new Date().getTime();
        this.state = 'fledgling';
        this.didConnect = false;

        // a queue for candidates waiting to go out. We try to amalgamate candidates into a single candidate message where possible
        this.candidateSendQueue = [];
        this.candidateSendTries = 0;
    }

    MatrixCall.CALL_TIMEOUT = 60000;

    MatrixCall.prototype.createPeerConnection = function() {
        var stunServer = 'stun:stun.l.google.com:19302';
        var pc;
        if (window.mozRTCPeerConnection) {
            pc = new window.mozRTCPeerConnection({'url': stunServer});
        } else {
            pc = new window.RTCPeerConnection({"iceServers":[{"urls":"stun:stun.l.google.com:19302"}]});
        }
        var self = this;
        pc.oniceconnectionstatechange = function() { self.onIceConnectionStateChanged(); };
        pc.onsignalingstatechange = function() { self.onSignallingStateChanged(); };
        pc.onicecandidate = function(c) { self.gotLocalIceCandidate(c); };
        pc.onaddstream = function(s) { self.onAddStream(s); };
        return pc;
    }

    MatrixCall.prototype.placeCall = function(config) {
        var self = this;
        matrixPhoneService.callPlaced(this);
        navigator.getUserMedia({audio: config.audio, video: config.video}, function(s) { self.gotUserMediaForInvite(s); }, function(e) { self.getUserMediaFailed(e); });
        this.state = 'wait_local_media';
        this.direction = 'outbound';
        this.config = config;
    };

    MatrixCall.prototype.initWithInvite = function(event) {
        this.msg = event.content;
        this.peerConn = this.createPeerConnection();
        this.peerConn.setRemoteDescription(new RTCSessionDescription(this.msg.offer), this.onSetRemoteDescriptionSuccess, this.onSetRemoteDescriptionError);
        this.state = 'ringing';
        this.direction = 'inbound';
        var self = this;
        $timeout(function() {
            if (self.state == 'ringing') {
                self.state = 'ended';
                self.hangupParty = 'remote'; // effectively
                self.stopAllMedia();
                if (self.peerConn.signalingState != 'closed') self.peerConn.close();
                if (self.onHangup) self.onHangup(self);
            }
        }, this.msg.lifetime - event.age);
    };

    // perverse as it may seem, sometimes we want to instantiate a call with a hangup message
    // (because when getting the state of the room on load, events come in reverse order and
    // we want to remember that a call has been hung up)
    MatrixCall.prototype.initWithHangup = function(event) {
        this.msg = event.content;
        this.state = 'ended';
    };

    MatrixCall.prototype.answer = function() {
        console.log("Answering call "+this.call_id);
        var self = this;
        if (!this.localAVStream && !this.waitForLocalAVStream) {
            navigator.getUserMedia({audio: true, video: false}, function(s) { self.gotUserMediaForAnswer(s); }, function(e) { self.getUserMediaFailed(e); });
            this.state = 'wait_local_media';
        } else if (this.localAVStream) {
            this.gotUserMediaForAnswer(this.localAVStream);
        } else if (this.waitForLocalAVStream) {
            this.state = 'wait_local_media';
        }
    };

    MatrixCall.prototype.stopAllMedia = function() {
        if (this.localAVStream) {
            forAllTracksOnStream(this.localAVStream, function(t) {
                if (t.stop) t.stop();
            });
        }
        if (this.remoteAVStream) {
            forAllTracksOnStream(this.remoteAVStream, function(t) {
                if (t.stop) t.stop();
            });
        }
    };

    MatrixCall.prototype.hangup = function(suppressEvent) {
        console.log("Ending call "+this.call_id);

        this.stopAllMedia();
        if (this.peerConn) this.peerConn.close();

        this.hangupParty = 'local';

        var content = {
            version: 0,
            call_id: this.call_id,
        };
        this.sendEventWithRetry('m.call.hangup', content);
        this.state = 'ended';
        if (this.onHangup && !suppressEvent) this.onHangup(this);
    };

    MatrixCall.prototype.gotUserMediaForInvite = function(stream) {
        if (this.successor) {
            this.successor.gotUserMediaForAnswer(stream);
            return;
        }
        if (this.state == 'ended') return;

        this.localAVStream = stream;
        var audioTracks = stream.getAudioTracks();
        for (var i = 0; i < audioTracks.length; i++) {
            audioTracks[i].enabled = true;
        }
        this.peerConn = this.createPeerConnection();
        this.peerConn.addStream(stream);
        var self = this;
        this.peerConn.createOffer(function(d) {
            self.gotLocalOffer(d);
        }, function(e) {
            self.getLocalOfferFailed(e);
        });
        $rootScope.$apply(function() {
            self.state = 'create_offer';
        });
    };

    MatrixCall.prototype.gotUserMediaForAnswer = function(stream) {
        if (this.state == 'ended') return;

        this.localAVStream = stream;
        var audioTracks = stream.getAudioTracks();
        for (var i = 0; i < audioTracks.length; i++) {
            audioTracks[i].enabled = true;
        }
        this.peerConn.addStream(stream);
        var self = this;
        var constraints = {
            'mandatory': {
                'OfferToReceiveAudio': true,
                'OfferToReceiveVideo': false
            },
        };
        this.peerConn.createAnswer(function(d) { self.createdAnswer(d); }, function(e) {}, constraints);
        // This can't be in an apply() because it's called by a predecessor call under glare conditions :(
        self.state = 'create_answer';
    };

    MatrixCall.prototype.gotLocalIceCandidate = function(event) {
        console.log(event);
        if (event.candidate) {
            this.sendCandidate(event.candidate);
        }
    }

    MatrixCall.prototype.gotRemoteIceCandidate = function(cand) {
        console.log("Got ICE candidate from remote: "+cand);
        if (this.state == 'ended') {
            console.log("Ignoring remote ICE candidate because call has ended");
            return;
        }
        this.peerConn.addIceCandidate(new RTCIceCandidate(cand), function() {}, function(e) {});
    };

    MatrixCall.prototype.receivedAnswer = function(msg) {
        if (this.state == 'ended') return;

        this.peerConn.setRemoteDescription(new RTCSessionDescription(msg.answer), this.onSetRemoteDescriptionSuccess, this.onSetRemoteDescriptionError);
        this.state = 'connecting';
    };

    MatrixCall.prototype.gotLocalOffer = function(description) {
        console.log("Created offer: "+description);

        if (this.state == 'ended') {
            console.log("Ignoring newly created offer on call ID "+this.call_id+" because the call has ended");
            return;
        }

        this.peerConn.setLocalDescription(description);

        var content = {
            version: 0,
            call_id: this.call_id,
            offer: description,
            lifetime: MatrixCall.CALL_TIMEOUT
        };
        this.sendEventWithRetry('m.call.invite', content);

        var self = this;
        $timeout(function() {
            if (self.state == 'invite_sent') {
                self.hangupReason = 'invite_timeout';
                self.hangup();
            }
        }, MatrixCall.CALL_TIMEOUT);

        $rootScope.$apply(function() {
            self.state = 'invite_sent';
        });
    };

    MatrixCall.prototype.createdAnswer = function(description) {
        console.log("Created answer: "+description);
        this.peerConn.setLocalDescription(description);
        var content = {
            version: 0,
            call_id: this.call_id,
            answer: description
        };
        this.sendEventWithRetry('m.call.answer', content);
        var self = this;
        $rootScope.$apply(function() {
            self.state = 'connecting';
        });
    };

    MatrixCall.prototype.getLocalOfferFailed = function(error) {
        this.onError("Failed to start audio for call!");
    };

    MatrixCall.prototype.getUserMediaFailed = function() {
        this.onError("Couldn't start capturing audio! Is your microphone set up?");
        this.hangup();
    };

    MatrixCall.prototype.onIceConnectionStateChanged = function() {
        if (this.state == 'ended') return; // because ICE can still complete as we're ending the call
        console.log("Ice connection state changed to: "+this.peerConn.iceConnectionState);
        // ideally we'd consider the call to be connected when we get media but chrome doesn't implement nay of the 'onstarted' events yet
        if (this.peerConn.iceConnectionState == 'completed' || this.peerConn.iceConnectionState == 'connected') {
            var self = this;
            $rootScope.$apply(function() {
                self.state = 'connected';
                self.didConnect = true;
            });
        }
    };

    MatrixCall.prototype.onSignallingStateChanged = function() {
        console.log("call "+this.call_id+": Signalling state changed to: "+this.peerConn.signalingState);
    };

    MatrixCall.prototype.onSetRemoteDescriptionSuccess = function() {
        console.log("Set remote description");
    };
    
    MatrixCall.prototype.onSetRemoteDescriptionError = function(e) {
        console.log("Failed to set remote description"+e);
    };

    MatrixCall.prototype.onAddStream = function(event) {
        console.log("Stream added"+event);

        var s = event.stream;

        this.remoteAVStream = s;

        var self = this;
        forAllTracksOnStream(s, function(t) {
            // not currently implemented in chrome
            t.onstarted = self.onRemoteStreamTrackStarted;
        });

        event.stream.onended = function(e) { self.onRemoteStreamEnded(e); }; 
        // not currently implemented in chrome
        event.stream.onstarted = function(e) { self.onRemoteStreamStarted(e); };
        var player = new Audio();
        player.src = URL.createObjectURL(s);
        player.play();
    };

    MatrixCall.prototype.onRemoteStreamStarted = function(event) {
        var self = this;
        $rootScope.$apply(function() {
            self.state = 'connected';
        });
    };

    MatrixCall.prototype.onRemoteStreamEnded = function(event) {
        console.log("Remote stream ended");
        var self = this;
        $rootScope.$apply(function() {
            self.state = 'ended';
            self.hangupParty = 'remote';
            self.stopAllMedia();
            if (self.peerConn.signalingState != 'closed') self.peerConn.close();
            if (self.onHangup) self.onHangup(self);
        });
    };

    MatrixCall.prototype.onRemoteStreamTrackStarted = function(event) {
        var self = this;
        $rootScope.$apply(function() {
            self.state = 'connected';
        });
    };

    MatrixCall.prototype.onHangupReceived = function() {
        console.log("Hangup received");
        this.state = 'ended';
        this.hangupParty = 'remote';
        this.stopAllMedia();
        if (this.peerConn.signalingState != 'closed') this.peerConn.close();
        if (this.onHangup) this.onHangup(this);
    };

    MatrixCall.prototype.replacedBy = function(newCall) {
        console.log(this.call_id+" being replaced by "+newCall.call_id);
        if (this.state == 'wait_local_media') {
            console.log("Telling new call to wait for local media");
            newCall.waitForLocalAVStream = true;
        } else if (this.state == 'create_offer') {
            console.log("Handing local stream to new call");
            newCall.localAVStream = this.localAVStream;
            delete(this.localAVStream);
        } else if (this.state == 'invite_sent') {
            console.log("Handing local stream to new call");
            newCall.localAVStream = this.localAVStream;
            delete(this.localAVStream);
        }
        this.successor = newCall;
        this.hangup(true);
    };

    MatrixCall.prototype.sendEventWithRetry = function(evType, content) {
        var ev = { type:evType, content:content, tries:1 };
        var self = this;
        matrixService.sendEvent(this.room_id, evType, undefined, content).then(this.eventSent, function(error) { self.eventSendFailed(ev, error); } );
    };

    MatrixCall.prototype.eventSent = function() {
    };

    MatrixCall.prototype.eventSendFailed = function(ev, error) {
        if (ev.tries > 5) {
            console.log("Failed to send event of type "+ev.type+" on attempt "+ev.tries+". Giving up.");
            return;
        }
        var delayMs = 500 * Math.pow(2, ev.tries);
        console.log("Failed to send event of type "+ev.type+". Retrying in "+delayMs+"ms");
        ++ev.tries;
        var self = this;
        $timeout(function() {
            matrixService.sendEvent(self.room_id, ev.type, undefined, ev.content).then(self.eventSent, function(error) { self.eventSendFailed(ev, error); } );
        }, delayMs);
    };

    // Sends candidates with are sent in a special way because we try to amalgamate them into one message
    MatrixCall.prototype.sendCandidate = function(content) {
        this.candidateSendQueue.push(content);
        var self = this;
        if (this.candidateSendTries == 0) $timeout(function() { self.sendCandidateQueue(); }, 100);
    };

    MatrixCall.prototype.sendCandidateQueue = function(content) {
        if (this.candidateSendQueue.length == 0) return;

        var cands = this.candidateSendQueue;
        this.candidateSendQueue = [];
        ++this.candidateSendTries;
        var content = {
            version: 0,
            call_id: this.call_id,
            candidates: cands
        };
        var self = this;
        console.log("Attempting to send "+cands.length+" candidates");
        matrixService.sendEvent(self.room_id, 'm.call.candidates', undefined, content).then(function() { self.candsSent(); }, function(error) { self.candsSendFailed(cands, error); } );
    };

    MatrixCall.prototype.candsSent = function() {
        this.candidateSendTries = 0;
        this.sendCandidateQueue();
    };

    MatrixCall.prototype.candsSendFailed = function(cands, error) {
        for (var i = 0; i < cands.length; ++i) {
            this.candidateSendQueue.push(cands[i]);
        }

        if (this.candidateSendTries > 5) {
            console.log("Failed to send candidates on attempt "+this.candidateSendTries+". Giving up for now.");
            this.candidateSendTries = 0;
            return;
        }

        var delayMs = 500 * Math.pow(2, this.candidateSendTries);
        ++this.candidateSendTries;
        console.log("Failed to send candidates. Retrying in "+delayMs+"ms");
        var self = this;
        $timeout(function() {
            self.sendCandidateQueue();
        }, delayMs);
    };

    return MatrixCall;
}]);
