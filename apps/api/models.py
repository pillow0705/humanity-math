from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TaskType = Literal['insight_explore','review','lean_work','research_synthesis']
class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
class Submission(Strict):
    contributor_id: str = Field(pattern=r'^[1-9][0-9]*$', description='Sequential contributor number encoded as a string')
    contributor_password: str | None = Field(default=None,pattern=r'^[1-9][0-9]{7}$',description='Private eight-digit credential, required for new accounts; never publish it')
    contributor_name: str = Field(min_length=2,max_length=32,pattern=r'^[A-Za-z][A-Za-z0-9_-]*$')
    task_id: str | None = None
    claim_token: str | None = None
    supersedes_id: str | None = None
class InlineLean(Strict):
    source: str = Field(min_length=1,max_length=200000)
    theorem: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$')
    dependencies: list[str] = Field(default_factory=list,max_length=50)
    toolchain: Literal['leanprover/lean4:v4.19.0'] = 'leanprover/lean4:v4.19.0'

class LeanTarget(Strict):
    definitions: str = Field(default='',max_length=50000,description='Ordinary Lean definitions/imports needed to state the fixed proposition; no proof placeholders or new axioms')
    expression: str = Field(min_length=1,max_length=20000,description='A complete Lean proposition, including all intended quantifiers and hypotheses; not a theorem declaration or proof')
    dependencies: list[str] = Field(default_factory=list,max_length=50,description='Previously verified artifact IDs needed by the proposition; frozen when registered')

class Insight(Submission):
    lean_target: LeanTarget

    is_prerequisite: bool = Field(default=False,strict=True,description='True only for a supporting lemma registered for the current exploration task: no separate insight base points; it does not complete the task')
    claims_problem_solved: int = Field(default=0,ge=-1,le=1,strict=True,description='Completion claim only: 0 for all intermediate insights; 1 for a complete proof of the original problem P; -1 for a complete proof of its negation. Independent of assigned research direction; not verification.')
    lean: InlineLean | None = None
    problem_id: str
    title: str = Field(min_length=1, max_length=300)
    claim: str = Field(min_length=1, max_length=20000)
    proof: str = Field(min_length=1, max_length=100000)
    proof_status: Literal['claimed_complete','partial','conjecture']
    gaps: list[str] = Field(default_factory=list, max_length=100)
    references: list[str] = Field(default_factory=list, max_length=100)
    @model_validator(mode='after')
    def complete_claim(self):
        if self.is_prerequisite and self.claims_problem_solved!=0:
            raise ValueError('A supporting prerequisite must use claims_problem_solved=0')
        if self.claims_problem_solved!=0 and (self.proof_status!='claimed_complete' or self.gaps):
            raise ValueError('A problem-completion claim requires claimed_complete and no declared gaps')
        return self
TargetAlignment = Literal['aligned','misaligned','uncertain','not_applicable']
class Review(Submission):
    target_alignment: TargetAlignment = Field(default='not_applicable',description='Required for an insight with a registered Lean target: does it faithfully express the mathematical claim?')
    target_hash: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$',description='Optional expected immutable target hash; the server records the actual hash with the review')
    verdict: Literal['support','concern','refute','needs_work']
    rationale: str = Field(min_length=1, max_length=30000)
    score: int = Field(ge=1, le=5, strict=True, description='Integer rating from 1 (lowest) to 5 (highest); not a proof certificate')
class ReviewItem(Strict):
    target_alignment: TargetAlignment = 'not_applicable'
    target_hash: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')
    insight_id: str = Field(pattern=r'^I[A-Za-z0-9_-]{1,99}$')
    verdict: Literal['support','concern','refute','needs_work']
    rationale: str = Field(min_length=1,max_length=30000)
    score: int = Field(ge=1,le=5,strict=True)
class ReviewBatch(Strict):
    contributor_id: str = Field(pattern=r'^[1-9][0-9]*$')
    contributor_password: str | None = Field(default=None,pattern=r'^[1-9][0-9]{7}$',description='Private eight-digit credential, required for new accounts; never publish it')
    contributor_name: str = Field(min_length=2,max_length=32,pattern=r'^[A-Za-z][A-Za-z0-9_-]*$')
    task_id: str
    claim_token: str
    reviews: list[ReviewItem] = Field(min_length=1,max_length=10)
    @field_validator('reviews')
    @classmethod
    def unique_targets(cls, value):
        if len({x.insight_id for x in value}) != len(value):
            raise ValueError('Each insight must appear exactly once')
        return value
class Claim(Strict):
    contributor_id: str = Field(pattern=r'^[1-9][0-9]*$', description='Sequential contributor number encoded as a string')
    contributor_password: str | None = Field(default=None,pattern=r'^[1-9][0-9]{7}$',description='Private eight-digit credential, required for new accounts; never publish it')
    contributor_name: str = Field(min_length=2,max_length=32,pattern=r'^[A-Za-z][A-Za-z0-9_-]*$')
    task_id: str | None = None
    problem_id: str | None = None
    type: TaskType | None = None
class Lease(Strict):
    claim_token: str
    reason: str = Field(default='', max_length=1000)
class Artifact(Submission):
    insight_id: str
    source: str = Field(min_length=1, max_length=200000)
    theorem: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$')
    dependencies: list[str] = Field(default_factory=list, max_length=50)
    toolchain: Literal['leanprover/lean4:v4.19.0'] = 'leanprover/lean4:v4.19.0'
class Audit(Submission):
    verdict: Literal['pass','concern','fail']
    rationale: str = Field(min_length=1, max_length=30000)
    locations: list[str] = Field(default_factory=list, max_length=100)
    reproduction: str = Field(default='', max_length=30000)
class Solution(Submission):
    claims_problem_solved: int = Field(default=1,ge=-1,le=1,strict=True,description='1: complete proof of P; -1: complete proof of its negation')
    @field_validator('claims_problem_solved')
    @classmethod
    def final_direction(cls, value):
        if value == 0: raise ValueError('A final solution must claim proof or disproof')
        return value
    proof: str = Field(min_length=1, max_length=100000)
    artifact_id: str

class ContributorClaim(Strict):
    name: str = Field(min_length=2,max_length=32,pattern=r'^[A-Za-z][A-Za-z0-9_-]*$',description='Unique English name; case-insensitive uniqueness')

class ContributorAction(Strict):
    contributor_id: str = Field(pattern=r'^[1-9][0-9]*$')
    contributor_password: str | None = Field(default=None,pattern=r'^[1-9][0-9]{7}$',description='Private eight-digit credential, required for new accounts; never publish it')
    contributor_name: str = Field(min_length=2,max_length=32,pattern=r'^[A-Za-z][A-Za-z0-9_-]*$')
    reason: str = Field(default='',max_length=2000)


class CredentialLogin(Strict):
    name: str = Field(min_length=2,max_length=32)
    password: str = Field(pattern=r'^[1-9][0-9]*$')

class CredentialChange(ContributorAction):
    new_password: str | None = Field(default=None,pattern=r'^[1-9][0-9]{7}$')

class ResearchPath(Strict):
    title: str = Field(min_length=1,max_length=300)
    strategy: str = Field(min_length=1,max_length=15000)
    insight_ids: list[str] = Field(min_length=1,max_length=300)
    proof_gaps: list[str] = Field(min_length=1,max_length=50)
    difficulties: str = Field(min_length=1,max_length=15000)
    next_steps: list[str] = Field(min_length=1,max_length=30)

class ResearchPlan(Submission):
    model_config = ConfigDict(json_schema_extra={'x-max-body-characters':10000})
    problem_id: str
    title: str = Field(min_length=1,max_length=300)
    summary: str = Field(min_length=1,max_length=20000)
    paths: list[ResearchPath] = Field(min_length=1,max_length=30)
    task_id: str
    claim_token: str
    @model_validator(mode='after')
    def concise_guide(self):
        texts=[self.title,self.summary]
        for path in self.paths:
            texts.extend([path.title,path.strategy,path.difficulties,*path.proof_gaps,*path.next_steps])
        if sum(len(text) for text in texts)>10000:
            raise ValueError('Research guide body must contain at most 10000 Unicode characters across title, summary and every path text field, including spaces and formulas; IDs and JSON syntax are excluded. Keep only key conclusions, citations, proof gaps and next steps.')
        return self


class LeanTargetRegistration(ContributorAction):
    lean_target: LeanTarget
