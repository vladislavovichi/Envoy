---------------------------- MODULE Negotiation ----------------------------
EXTENDS Naturals, FiniteSets

CONSTANTS Offers, None

Sides == {"A", "B"}
Coordinator == "A"
Participant == "B"
TieWinner == "A"
Other(p) == IF p = "A" THEN "B" ELSE "A"
Settled == {"EXEC", "ABORTED", "REVOKED", "DISPUTED", "CLOSED"}

VARIABLES phase, lastSent, lastRecv, answered, agreed, myReady, peerReady,
          decision, msgs, used, checklist, accepts

vars == <<phase, lastSent, lastRecv, answered, agreed, myReady, peerReady,
          decision, msgs, used, checklist, accepts>>

Msg(t, to, h, pv, d) == [type |-> t, to |-> to, hash |-> h, prev |-> pv, d |-> d]

Init ==
    /\ phase = [p \in Sides |-> "NEG"]
    /\ lastSent = [p \in Sides |-> None]
    /\ lastRecv = [p \in Sides |-> None]
    /\ answered = [p \in Sides |-> {}]
    /\ agreed = [p \in Sides |-> None]
    /\ myReady = [p \in Sides |-> FALSE]
    /\ peerReady = [p \in Sides |-> FALSE]
    /\ decision = [p \in Sides |-> None]
    /\ msgs = {}
    /\ used = [p \in Sides |-> {}]
    /\ checklist = [p \in Sides |-> 0]
    /\ accepts = [p \in Sides |-> 0]

Send(m) == msgs' = msgs \cup {m}

Offer(p, o) ==
    /\ phase[p] = "NEG"
    /\ o \notin used[p]
    /\ ~(lastSent[p] # None /\ lastRecv[p] = None)
    /\ o # lastRecv[p]
    /\ used' = [used EXCEPT ![p] = @ \cup {o}]
    /\ answered' = [answered EXCEPT ![p] = IF lastRecv[p] = None THEN @ ELSE @ \cup {lastRecv[p]}]
    /\ lastSent' = [lastSent EXCEPT ![p] = o]
    /\ lastRecv' = [lastRecv EXCEPT ![p] = None]
    /\ Send(Msg("offer", Other(p), o, lastRecv[p], None))
    /\ UNCHANGED <<phase, agreed, myReady, peerReady, decision, checklist, accepts>>

RecvOffer(p, m) ==
    /\ m.type = "offer" /\ m.to = p /\ phase[p] = "NEG"
    /\ m.hash \notin answered[p] /\ m.hash # lastRecv[p]
    /\ ~(lastSent[p] # None /\ m.prev # lastSent[p] /\ p = TieWinner)
    /\ lastSent' = [lastSent EXCEPT ![p] = None]
    /\ lastRecv' = [lastRecv EXCEPT ![p] = m.hash]
    /\ UNCHANGED <<phase, answered, agreed, myReady, peerReady, decision, msgs, used,
                   checklist, accepts>>

Accept(p) ==
    /\ phase[p] = "NEG" /\ lastRecv[p] # None /\ lastRecv[p] \notin answered[p]
    /\ phase' = [phase EXCEPT ![p] = "AGREED"]
    /\ agreed' = [agreed EXCEPT ![p] = lastRecv[p]]
    /\ answered' = [answered EXCEPT ![p] = @ \cup {lastRecv[p]}]
    /\ lastRecv' = [lastRecv EXCEPT ![p] = None]
    /\ accepts' = [accepts EXCEPT ![p] = @ + 1]
    /\ Send(Msg("accept", Other(p), lastRecv[p], None, None))
    /\ UNCHANGED <<lastSent, myReady, peerReady, decision, used, checklist>>

RecvAccept(p, m) ==
    /\ m.type = "accept" /\ m.to = p /\ phase[p] = "NEG" /\ lastSent[p] = m.hash
    /\ phase' = [phase EXCEPT ![p] = "AGREED"]
    /\ agreed' = [agreed EXCEPT ![p] = m.hash]
    /\ lastSent' = [lastSent EXCEPT ![p] = None]
    /\ UNCHANGED <<lastRecv, answered, myReady, peerReady, decision, msgs, used,
                   checklist, accepts>>

Reject(p) ==
    /\ phase[p] = "NEG"
    /\ phase' = [phase EXCEPT ![p] = "CLOSED"]
    /\ Send(Msg("reject", Other(p), None, None, None))
    /\ UNCHANGED <<lastSent, lastRecv, answered, agreed, myReady, peerReady, decision, used,
                   checklist, accepts>>

RecvRejectWhileNegotiating(p, m) ==
    /\ m.type = "reject" /\ m.to = p /\ phase[p] = "NEG"
    /\ phase' = [phase EXCEPT ![p] = "CLOSED"]
    /\ UNCHANGED <<lastSent, lastRecv, answered, agreed, myReady, peerReady, decision, msgs,
                   used, checklist, accepts>>

Decide(d) ==
    /\ decision' = [decision EXCEPT ![Coordinator] = d]
    /\ phase' = [phase EXCEPT ![Coordinator] = IF d = "commit" THEN "EXEC" ELSE "ABORTED"]
    /\ checklist' = [checklist EXCEPT ![Coordinator] = IF d = "commit" THEN @ + 1 ELSE @]
    /\ Send(Msg("decision", Participant, agreed[Coordinator], None, d))

Open(p) == phase[p] \in {"AGREED", "COMMIT"}

ReadyCoordinator ==
    /\ Open(Coordinator) /\ ~myReady[Coordinator]
    /\ myReady' = [myReady EXCEPT ![Coordinator] = TRUE]
    /\ IF peerReady[Coordinator]
       THEN Decide("commit")
       ELSE /\ phase' = [phase EXCEPT ![Coordinator] = "COMMIT"]
            /\ UNCHANGED <<decision, checklist, msgs>>
    /\ UNCHANGED <<lastSent, lastRecv, answered, agreed, peerReady, used, accepts>>

RecvReady(m) ==
    /\ m.type = "ready" /\ m.to = Coordinator /\ Open(Coordinator)
    /\ m.hash = agreed[Coordinator] /\ ~peerReady[Coordinator]
    /\ peerReady' = [peerReady EXCEPT ![Coordinator] = TRUE]
    /\ IF myReady[Coordinator]
       THEN Decide("commit")
       ELSE UNCHANGED <<phase, decision, checklist, msgs>>
    /\ UNCHANGED <<lastSent, lastRecv, answered, agreed, myReady, used, accepts>>

AbortCoordinator ==
    /\ Open(Coordinator)
    /\ Decide("abort")
    /\ UNCHANGED <<lastSent, lastRecv, answered, agreed, myReady, peerReady, used, accepts>>

RecvRevokeCoordinator(m) ==
    /\ m.to = Coordinator /\ m.type \in {"revoke", "reject"}
    /\ Open(Coordinator) /\ (m.type = "reject" \/ m.hash = agreed[Coordinator])
    /\ Decide("abort")
    /\ UNCHANGED <<lastSent, lastRecv, answered, agreed, myReady, peerReady, used, accepts>>

AnswerRequest(m) ==
    /\ m.type = "request" /\ m.to = Coordinator /\ phase[Coordinator] \in Settled
    /\ LET d == IF decision[Coordinator] = "commit" /\ agreed[Coordinator] = m.hash
                THEN "commit" ELSE "abort"
       IN Send(Msg("decision", Participant, m.hash, None, d))
    /\ UNCHANGED <<phase, lastSent, lastRecv, answered, agreed, myReady, peerReady, decision,
                   used, checklist, accepts>>

ReadyParticipant ==
    /\ Open(Participant) /\ ~myReady[Participant]
    /\ myReady' = [myReady EXCEPT ![Participant] = TRUE]
    /\ phase' = [phase EXCEPT ![Participant] = "COMMIT"]
    /\ Send(Msg("ready", Coordinator, agreed[Participant], None, None))
    /\ UNCHANGED <<lastSent, lastRecv, answered, agreed, peerReady, decision, used,
                   checklist, accepts>>

RevokeParticipant ==
    /\ Open(Participant)
    /\ Send(Msg("revoke", Coordinator, agreed[Participant], None, None))
    /\ IF myReady[Participant]
       THEN UNCHANGED phase
       ELSE phase' = [phase EXCEPT ![Participant] = "REVOKED"]
    /\ UNCHANGED <<lastSent, lastRecv, answered, agreed, myReady, peerReady, decision, used,
                   checklist, accepts>>

RequestDecision ==
    /\ phase[Participant] = "COMMIT" /\ myReady[Participant]
    /\ Send(Msg("request", Coordinator, agreed[Participant], None, None))
    /\ UNCHANGED <<phase, lastSent, lastRecv, answered, agreed, myReady, peerReady, decision,
                   used, checklist, accepts>>

RecvDecision(m) ==
    /\ m.type = "decision" /\ m.to = Participant /\ Open(Participant)
    /\ m.hash = agreed[Participant]
    /\ (m.d = "abort" \/ myReady[Participant])
    /\ decision' = [decision EXCEPT ![Participant] = m.d]
    /\ phase' = [phase EXCEPT ![Participant] = IF m.d = "commit" THEN "EXEC" ELSE "ABORTED"]
    /\ checklist' = [checklist EXCEPT ![Participant] = IF m.d = "commit" THEN @ + 1 ELSE @]
    /\ UNCHANGED <<lastSent, lastRecv, answered, agreed, myReady, peerReady, msgs, used,
                   accepts>>

RecvRevokeParticipant(m) ==
    /\ m.to = Participant /\ m.type = "reject" /\ Open(Participant)
    /\ decision' = [decision EXCEPT ![Participant] = "abort"]
    /\ phase' = [phase EXCEPT ![Participant] = "ABORTED"]
    /\ UNCHANGED <<lastSent, lastRecv, answered, agreed, myReady, peerReady, msgs, used,
                   checklist, accepts>>

Dispute(p) ==
    /\ phase[p] = "EXEC"
    /\ phase' = [phase EXCEPT ![p] = "DISPUTED"]
    /\ Send(Msg("dispute", Other(p), agreed[p], None, None))
    /\ UNCHANGED <<lastSent, lastRecv, answered, agreed, myReady, peerReady, decision, used,
                   checklist, accepts>>

RecvDispute(p, m) ==
    /\ m.type = "dispute" /\ m.to = p /\ phase[p] = "EXEC" /\ m.hash = agreed[p]
    /\ phase' = [phase EXCEPT ![p] = "DISPUTED"]
    /\ UNCHANGED <<lastSent, lastRecv, answered, agreed, myReady, peerReady, decision, msgs,
                   used, checklist, accepts>>

Drop(m) ==
    /\ msgs' = msgs \ {m}
    /\ UNCHANGED <<phase, lastSent, lastRecv, answered, agreed, myReady, peerReady, decision,
                   used, checklist, accepts>>

Quiescent ==
    /\ \A p \in Sides : phase[p] \in Settled
    /\ msgs = {}
    /\ UNCHANGED vars

Next ==
    \/ \E p \in Sides, o \in Offers : Offer(p, o)
    \/ \E p \in Sides : Accept(p) \/ Reject(p) \/ Dispute(p)
    \/ \E p \in Sides, m \in msgs :
          RecvOffer(p, m) \/ RecvAccept(p, m) \/ RecvRejectWhileNegotiating(p, m)
          \/ RecvDispute(p, m)
    \/ ReadyCoordinator \/ AbortCoordinator \/ ReadyParticipant \/ RevokeParticipant
    \/ RequestDecision
    \/ \E m \in msgs :
          RecvReady(m) \/ RecvRevokeCoordinator(m) \/ AnswerRequest(m)
          \/ RecvDecision(m) \/ RecvRevokeParticipant(m) \/ Drop(m)
    \/ Quiescent

Spec == Init /\ [][Next]_vars

Fairness ==
    /\ WF_vars(AbortCoordinator)
    /\ WF_vars(Reject(Coordinator))
    /\ SF_vars(RequestDecision)
    /\ SF_vars(\E m \in msgs : AnswerRequest(m))
    /\ SF_vars(\E m \in msgs : RecvDecision(m))

LiveSpec == Spec /\ Fairness

Agreement ==
    phase["A"] = "EXEC" /\ phase["B"] = "EXEC" => agreed["A"] = agreed["B"]

AllOrNothing ==
    \A p \in Sides :
        decision[p] = "commit" => decision[Other(p)] # "abort" /\ phase[Other(p)] # "REVOKED"

SameCommittedOffer ==
    decision["A"] = "commit" /\ decision["B"] = "commit" => agreed["A"] = agreed["B"]

OneChecklist == \A p \in Sides : checklist[p] <= 1

NoDoubleAccept == \A p \in Sides : accepts[p] <= 1

CommitNeedsReady == decision["A"] = "commit" => myReady["B"]

ReadyResolves ==
    (phase["B"] = "COMMIT" /\ myReady["B"]) ~> (phase["B"] \in Settled)

=============================================================================
