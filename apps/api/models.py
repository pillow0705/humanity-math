from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TaskType = Literal['insight_explore','review','lean_work','research_synthesis','lean_target']
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
    dependencies: list[str] = Field(default_factory=list,max_length=50,description='Previously verified proof artifact IDs needed by the proposition; frozen when registered')
    insight_dependencies: list[str] = Field(default_factory=list,max_length=100,description='Insight IDs whose valid fixed target definitions are imported; these imports do not supply proofs')

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
    score: int = Field(ge=0, le=5, strict=True, description='0 only for non-mathematical or wholly unrelated content: an independent zero review immediately trashes the insight. Otherwise 1–5; not a proof certificate')
class ReviewItem(Strict):
    target_alignment: TargetAlignment = 'not_applicable'
    target_hash: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')
    insight_id: str = Field(pattern=r'^I[A-Za-z0-9_-]{1,99}$')
    verdict: Literal['support','concern','refute','needs_work']
    rationale: str = Field(min_length=1,max_length=30000)
    score: int = Field(ge=0,le=5,strict=True)
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

class RoadmapNode(Strict):
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,79}$')
    kind: Literal['insight','lemma','goal']
    statement: str = Field(default='',max_length=3000)
    status: Literal['supported','conditional','missing','target']
    insight_ids: list[str] = Field(default_factory=list,max_length=100)

class RoadmapStep(Strict):
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,79}$')
    requires: list[str] = Field(min_length=1,max_length=100,description='All of these premises are needed together; separate steps to the same conclusion are alternatives')
    concludes: str
    explanation: str = Field(min_length=1,max_length=2000)

class MissingBlock(Strict):
    node_id: str
    description: str = Field(min_length=1,max_length=2000)
    next_step: str = Field(min_length=1,max_length=2000)

class ProofGraph(Strict):
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,79}$')
    title: str = Field(min_length=1,max_length=300)
    approach: str = Field(min_length=1,max_length=2000)
    goal_node_id: str
    nodes: list[RoadmapNode] = Field(min_length=2,max_length=100)
    steps: list[RoadmapStep] = Field(min_length=1,max_length=100)
    missing: list[MissingBlock] = Field(default_factory=list,max_length=100)

class GuideHeader(Strict):
    format: Literal['humanity-proof-graphs-v1'] = 'humanity-proof-graphs-v1'
    problem_id: str
    graphs: list[ProofGraph] = Field(min_length=1,max_length=30,description='Multiple substantially different proof approaches; no four-route cap. Merge overlapping or cosmetically renamed routes.')

class ResearchPlan(Submission):
    model_config = ConfigDict(json_schema_extra={'x-max-document-characters':40000})
    problem_id: str
    base_hash: str = Field(pattern=r'^[0-9a-f]{64}$',description='The shared guide hash supplied with this task; prevents overwriting another editor')
    header: GuideHeader
    body_markdown: str = Field(min_length=1,max_length=40000)
    task_id: str
    claim_token: str
    @model_validator(mode='after')
    def bounded_shared_document(self):
        from apps.api.modules.guide_format import render_document,MAX_DOCUMENT_CHARS
        if self.header.problem_id!=self.problem_id:raise ValueError('The document header must identify the same problem')
        if len(render_document(self.header.model_dump(),self.body_markdown))>MAX_DOCUMENT_CHARS:raise ValueError('The complete research guide, including JSON graph header, IDs, formatting and Markdown, must not exceed 40000 Unicode characters')
        return self


class LeanTargetRegistration(ContributorAction):
    lean_target: LeanTarget


class LeanTargetItem(Strict):
    insight_id: str = Field(pattern=r'^I[A-Za-z0-9_-]{1,99}$')
    lean_target: LeanTarget

class LeanTargetBatch(ContributorAction):
    task_id: str
    claim_token: str
    targets: list[LeanTargetItem] = Field(min_length=1,max_length=5)
    @field_validator('targets')
    @classmethod
    def unique_insights(cls,value):
        if len({x.insight_id for x in value})!=len(value): raise ValueError('Each assigned insight must appear exactly once')
        return value


class BoardMessage(ContributorAction):
    body: str = Field(min_length=1,max_length=1000,description='A short human message; Chinese and English are both welcome. No contribution points are awarded.')
