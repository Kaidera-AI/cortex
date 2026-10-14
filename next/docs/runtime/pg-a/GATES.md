# Acceptance gates
- [ ] G1: explicit narrow CONNECT ruling; new matrix differs only by that cell and explanation, hash recomputed. CHECK: supplemental static gate. EXPECT: one exact capability delta.
- [ ] G2: own branch from frozen30338b51, all frozen oracle bytes and original19 hashes unchanged; production manifest remains19. CHECK: Git byte/hash check. EXPECT: all exact matches.
- [ ] G3: draft SQL real transaction applies atomically to original19, map/lease tables FORCE RLS, three real guards/ACL closure; only owned actual LOGIN binding admits. CHECK: separately authored real-PG controls. EXPECT: deciding BODY assertions, zero setup errors.
- [ ] G4: 105 original frozen nodes loaded/run with per-node phase/hash receipts; no draft application implies no fabricated new boundary GREEN. CHECK: unchanged run_l1/worker. EXPECT: 105 exact loaded/executed, no errors/skips, explicit PASS/semantic/surface counts.
- [ ] G5: CONNECT size positive, revoke42501, no membership/LOGIN/CREATE/grantoption/other-db privilege broadening; causal SQL guard/ACL/fence mutations fail and exact restore passes. CHECK: added controls. EXPECT: intended BODY kills plus full restored affected gate.
- [ ] G6: one allowed lab session <=4h, owned resources/workers drained/source removed and deallocated before push. CHECK: receipt and lab-host status. EXPECT: cleanup PASS/deallocated.
- [ ] G7: normal own branch push/readback; explicit PG-B/C backlog and no prefix20 admission. CHECK: remote and sealed return. EXPECT: exact head/INDEX and returned:true.
Independent source/trust acceptance is held for Kai; it is not a self-check gate.
