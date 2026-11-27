"""Research guidance is independent from a mathematical completion claim."""
import secrets

def research_policy(problem):
    prove_only=(problem or {}).get('id') in ('P007','P009')
    return dict(prove_probability=1.0 if prove_only else 0.7,
                disprove_probability=0.0 if prove_only else 0.3,
                allowed_completion_claims=[0,1] if prove_only else [-1,0,1])

ENGLISH_PROBLEMS={
    'P007':dict(title="Irrationality of Euler's constant",statement=r"Let $\gamma=\lim_{n\to\infty}(H_n-\log n)$, where $H_n=\sum_{k=1}^n1/k$. Prove the statement $P$: $\gamma\notin\mathbb Q$."),
    'P009':dict(title='Irrationality of pi + e',statement=r"Let $\pi$ denote the circle constant and let $e=\exp(1)$. Prove the statement $P$: $\pi+e\notin\mathbb Q$."),
    'P003':dict(title='Collatz conjecture',statement=r"For nonnegative integers, define $T(n)=n/2$ when $n$ is even and $T(n)=3n+1$ when $n$ is odd. The statement $P$ is: for every positive integer $n$, there exists an integer $k\ge0$ such that $T^k(n)=1$."),
}
def describe_problem(problem):
    result=dict(problem,research_policy=research_policy(problem),collaboration_language='en')
    result.update(ENGLISH_PROBLEMS.get(problem['id'],{}))
    return result

def validate_completion_claim(problem,claim):
    from fastapi import HTTPException
    if claim not in research_policy(problem)['allowed_completion_claims']:
        raise HTTPException(422,'This problem accepts only 0 (intermediate insight) or 1 (complete proof of irrationality); -1 is disabled')

def assign_direction(problem=None):
    if research_policy(problem)['disprove_probability']==0: return 'prove'
    return 'prove' if secrets.randbelow(10) < 7 else 'disprove'

def direction_instruction(direction):
    if direction == 'prove':
        return 'Assigned direction: PROVE. Try to establish the original problem statement P through rigorous intermediate results and a complete argument.'
    return 'Assigned direction: DISPROVE. Try to prove the negation of P; for a universal statement seek a rigorously verified counterexample. For an irrationality claim, prove rationality exactly.'

def final_target(problem, claim=1):
    if type(claim) is not int or claim not in (-1, 1):
        raise ValueError('Final verification requires a completion claim of 1 or -1')
    if claim not in research_policy(problem)['allowed_completion_claims']:
        raise ValueError('Disproof completion is disabled for this problem')
    target=problem.get('lean_target')
    if not target: return None
    if claim == 1: return dict(target)
    return dict(target, expression='¬ ('+target['expression']+')')
