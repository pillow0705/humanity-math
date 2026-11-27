"""Read-time contribution calculation: quality, citations and formalization."""
BASE={'insights':10,'review':6,'lean_work':8,'research_synthesis':15,'lean_target':8}
VERSION='contribution-v8-reviewed-insights'
FINAL_PROOF_BONUS=3.0  # Initial configurable bonus: 30% of insight base points.

def policy():
    return dict(version=VERSION,base_points=BASE,prerequisite_base_points=0,exploration_credit='One main insight receives the ten-point base; supporting prerequisites registered with is_prerequisite=true under the same exploration lease receive no separate base. Main/prerequisite internal citations earn no citation bonus; external citations retain the usual one-point reward for the cited author.',review_adjustment='Without reviews: 10; otherwise 5 + 25 * ((mean(valid scores 1–5) - 1) / 4) ** 3; prerequisite base remains zero',review_range=[0,5],review_points_range=[5,30],zero_review='A non-self zero review immediately withdraws the irrelevant insight to trash; no contribution or citation rewards while withdrawn',review_prior_count=0,citation_points=1,citation_cap=None,citation_rule='Each distinct active insight referencing another insight outside its contribution group adds one point; repeated references in one result count once. Replaced results do not multiply citations.',lean_verified_insight_bonus=10,lean_bonus_rule='Once per insight with a proof verified against its immutable, valid Lean target; unbound historical compilations and unrelated probes do not qualify.',final_proof_insight_bonus=FINAL_PROOF_BONUS,final_proof_rule='Once per insight actually used in a verified final Lean proof, based on proof-term dependencies, not mere imports.',review_batch_size=10,review_batch_points='Six base points per batch, divided equally among its assigned reviews; self/duplicate/replaced reviews do not add points',lean_points='Code: eight points once per insight, awarded only to its first exact-target verified proof; concurrent or historical duplicate proofs earn zero. Audit report: eight points on submission. No waiting is required.',review_rules='Exclude self-reviews from ratings; latest review per registered contributor and target. One review/audit contribution per contributor and target.',replacement_rules='A replacement by the same contributor replaces the prior contribution; old content remains readable.',solution_points='No separate bonus for the final submission itself',identity='Claimed public English name plus sequential number')

class Scores:
    def __init__(self,tx):
        self.withdrawn={x['object_id'] for x in tx.all('submission_states') if x.get('withdrawn')}
        self.people={p['id']:p for p in tx.all('contributors')}
        self.objects={k:{x['id']:x for x in tx.all(k)} for k in ('insights','reviews','audits','lean_artifacts','solutions','research_plans','research_updates','target_batches')}
        self.verifications={x['id']:x for x in tx.all('verifications')}
        self.targets={x['insight_id']:x for x in tx.all('insight_targets')}
        from apps.api.modules.targets import target_proof_matches
        self.bound_proofs={a['id'] for a in self.objects['lean_artifacts'].values() if target_proof_matches(a,self.verifications.get(a['verification_id'],{}),self.targets.get(a['insight_id']))}
        self.events=tx.all('contributions');self.latest_reviews={};self.latest_audits={};self.superseded=set()
        for items in self.objects.values():
            for x in items.values():
                old=items.get(x.get('supersedes_id'))
                if old and old['contributor_id']==x['contributor_id']: self.superseded.add(old['id'])
        for x in sorted(self.objects['reviews'].values(),key=lambda x:(x['created_at'],x['id'])):
            if x['contributor_id'] in self.people and x['id'] not in self.withdrawn: self.latest_reviews[x['target_id'],x['contributor_id']]=x
        for x in sorted(self.objects['audits'].values(),key=lambda x:(x['created_at'],x['id'])):
            if x['contributor_id'] in self.people and x['id'] not in self.withdrawn: self.latest_audits[x['artifact_id'],x['contributor_id']]=x
        self.citations={}
        for kind in ('insights',):
            for obj in self.objects[kind].values():
                if obj['id'] in self.superseded or obj['id'] in self.withdrawn: continue
                for ref in set(obj.get('references',[])):
                    target=self.objects['insights'].get(ref)
                    if target and ref!=obj['id']:
                        if set(obj.get('contribution_group_ids',[])) & set(target.get('contribution_group_ids',[])): continue
                        self.citations.setdefault(ref,set()).add(obj['id'])
        self.verified_insights={a['insight_id'] for a in self.objects['lean_artifacts'].values() if a['id'] in self.bound_proofs}
        # Select across all historical passed artifacts, including withdrawn/replaced
        # records, so withdrawing a winner cannot unlock another reward.
        self.first_lean_proofs={}
        for v in sorted(self.verifications.values(),key=lambda v:(v.get('finished_at') or v.get('created_at',0),v['id'])):
            a=self.objects['lean_artifacts'].get(v.get('artifact_id'))
            if a and a['id'] in self.bound_proofs:
                self.first_lean_proofs.setdefault(a['insight_id'],a['id'])
        self.final_used=set()
        for sol in self.objects['solutions'].values():
            if sol['status']=='verified': self.final_used.update(sol.get('used_insight_ids',[]))
        self.memo={}
    def evaluation(self,obj,kind):
        if obj['id'] in self.memo: return self.memo[obj['id']]
        valid=[x for (target,person),x in self.latest_reviews.items() if target==obj['id'] and person!=obj['contributor_id'] and type(x.get('score')) is int and 1<=x['score']<=5]
        n=len(valid);b=0 if obj.get('is_prerequisite') else BASE[kind]
        adjusted=0 if not b else (5+25*((sum(x['score'] for x in valid)/n-1)/4)**3 if n else b)
        multiplier=adjusted/b if b else 1
        citation_points=len(self.citations.get(obj['id'],set())) if kind=='insights' else 0
        lean_bonus=10 if kind=='insights' and obj['id'] in self.verified_insights else 0
        final_bonus=FINAL_PROOF_BONUS if kind=='insights' and obj['id'] in self.final_used else 0
        result=dict(base_points=b,effective_reviews=n,average_review_score=round(sum(x['score'] for x in valid)/n,3) if n else None,multiplier=round(multiplier,6),review_adjusted_points=round(adjusted,6),citation_points=citation_points,lean_bonus=lean_bonus,final_proof_bonus=final_bonus,contribution_points=round(adjusted+citation_points+lean_bonus+final_bonus,6),algorithm=VERSION)
        if obj['id'] in self.withdrawn: result.update(contribution_points=0,withdrawn=True)
        self.memo[obj['id']]=result;return result
    def event(self,e):
        kind=e['kind'];b=BASE.get(kind,0)
        collection=e.get('object_kind') or {'review':'reviews','solution':'solutions'}.get(kind,kind)
        obj=self.objects.get(collection,{}).get(e['object_id'])
        if kind=='review' and obj and obj.get('batch_size'):
            b=BASE['review']/obj['batch_size']
        result=dict(e,base_points=b,score=0.0,pending_points=0.0,algorithm=VERSION,status='counted')
        if not obj: return dict(result,status='missing_result')
        if obj['id'] in self.withdrawn: return dict(result,status='withdrawn')
        if obj['contributor_id'] not in self.people: return dict(result,status='unregistered')
        if obj['id'] in self.superseded: return dict(result,status='superseded')
        if kind=='insights':
            ev=self.evaluation(obj,kind);return dict(result,base_points=ev['base_points'],score=ev['contribution_points'],evaluation=ev)
        if kind=='lean_target' and collection=='target_batches':
            eligible=[ident for ident in obj['target_ids'] if ident not in self.withdrawn|self.superseded and self.targets.get(ident,{}).get('target_hash')==obj['target_hashes'].get(ident) and self.targets.get(ident,{}).get('migration_batch_id')==obj['id']]
            valid=sum(self.targets[ident]['status']=='valid' for ident in eligible)
            waiting=sum(self.targets[ident]['status'] in ('queued','checking','error') for ident in eligible)
            return dict(result,score=round(8*valid/obj['batch_size'],6),pending_points=round(8*waiting/obj['batch_size'],6),status='counted' if valid else 'pending_target_check')
        if kind=='lean_work' and collection=='lean_artifacts':
            status=self.verifications.get(obj['verification_id'],{}).get('status','queued')
            winner=self.first_lean_proofs.get(obj['insight_id'])
            if winner and winner!=obj['id']: return dict(result,status='duplicate_formalization')
            if status=='passed': return dict(result,score=float(b),status='counted') if obj['id'] in self.bound_proofs else dict(result,status='unbound_target')
            return dict(result,status='pending_verification' if status in ('queued','running') else 'verification_failed',pending_points=float(b) if status in ('queued','running') else 0.0)
        if kind=='review':
            target=self.objects[obj['target_kind']][obj['target_id']]
            if target['contributor_id']==obj['contributor_id']: return dict(result,status='self_review')
            latest=self.latest_reviews.get((obj['target_id'],obj['contributor_id']))
            if not latest or latest['id']!=obj['id']: return dict(result,status='replaced_review')
        if kind=='lean_work' and collection=='audits':
            target=self.objects['lean_artifacts'][obj['artifact_id']]
            if target['insight_id'] in self.withdrawn:return dict(result,status='withdrawn_target')
            if target['contributor_id']==obj['contributor_id']: return dict(result,status='self_audit')
            latest=self.latest_audits.get((obj['artifact_id'],obj['contributor_id']))
            if not latest or latest['id']!=obj['id']: return dict(result,status='replaced_audit')
        return dict(result,score=float(b),status='counted' if b else 'bonus_not_configured')
    def records(self,person,problem_id=None):
        return [self.event(e) for e in self.events if e['contributor_id']==person and (not problem_id or e['problem_id']==problem_id)]
    def total(self,person,problem_id=None):
        records=self.records(person,problem_id)
        profile=self.people.get(person,{})
        demo=profile.get('demo_score',0) if profile.get('is_demo') and (not problem_id or problem_id==profile.get('demo_problem_id')) else 0
        by_kind={k:round(sum(e['score'] for e in records if e['kind']==k),6) for k in BASE}
        return dict(contributor_id=person,score=round(sum(e['score'] for e in records)+demo,6),demo_points=demo,pending_points=round(sum(e['pending_points'] for e in records),6),by_kind=by_kind,submissions=len(records),counted_submissions=sum(e['status']=='counted' for e in records),status='active',algorithm=VERSION)
