"""Idempotent local operator seed. No public problem-editing endpoint."""
from apps.api.infrastructure.store import Store, digest
from apps.api.modules.core import task

DATA = [('P007',
  '欧拉常数无理性问题',
  '令 $\\gamma=\\lim_{n\\to\\infty}(H_n-\\log n)$，其中 $H_n=\\sum_{k=1}^n1/k$。原命题 $P$：$\\gamma\\notin\\mathbb Q$。',
  '研究整数线性形式的非零性与误差，避免把高精度逼近当作无理性证明。',
  '寻找整数 $a_n,b_n$ 使 $0<|a_n\\gamma-b_n|\\to0$。分别检查整数性、非零性、误差收敛三个环节。可从积分表示、Euler–Maclaurin 展开与 Padé 逼近入手。若 '
  '$\\gamma=r/s$，非零整数线性形式至少为 $1/s$，从而得到矛盾。',
  '欧拉常数定义序列单调且有下界',
  '序列 $u_n=H_n-\\log n$ 严格递减且为正，因此存在有限极限。',
  '由 $\\log(1+1/n)=\\int_n^{n+1}dx/x>1/(n+1)$ 得 $u_{n+1}-u_n<0$。另一方面 $H_n>\\int_1^{n+1}dx/x=\\log(n+1)$，故 '
  '$u_n>\\log(1+1/n)>0$。单调有界定理给出收敛。'),
 ('P009',
  'π + e 无理性问题',
  '令 $\\pi$ 为圆周率，$e=\\exp(1)$。原命题 $P$：$\\pi+e\\notin\\mathbb Q$。',
  '研究两个常数的联合有理逼近与整数线性形式，寻找可严格控制的非零性和误差。',
  '寻找整数 $a_n,b_n$，使 $0<|a_n(\\pi+e)-b_n|\\to0$，并分别证明整数性、非零性和收敛。也可假设 $\\pi+e=r/s$，研究它与已知积分或级数表示的兼容性。调研涉及 $\\pi$ 与 $e$ '
  '的不同逼近方法；两者分别无理不能直接推出和无理。证伪方向需要证明精确有理等式，而非数值接近。',
  'π + e 与 π − e 不可能同时为有理数',
  '至少有一个数 $\\pi+e$、$\\pi-e$ 是无理数。',
  '若两者均为有理数，则其和 $2\\pi$ 为有理数，除以二可得 $\\pi$ 为有理数，与 $\\pi$ 的无理性矛盾。此结论不确定其中哪一个无理，因而尚未解决原命题。'),
 ('P003',
  'Collatz 猜想',
  '对非负整数定义 $T(n)=n/2$（$n$ 偶），$T(n)=3n+1$（$n$ 奇）。原命题 $P$：对每个正整数 $n$，存在整数 $k\\ge0$，使 $T^k(n)=1$。',
  '以奇数到奇数的加速映射研究下降条件，并严格区分平均行为和逐点终止。',
  '定义 $U(n)=(3n+1)/2^{v_2(3n+1)}$，其中 $n$ 为正奇数。先证明可直接下降的同余类，再分析其余类的有限步行为。随机漂移直觉只用于提出命题；必须处理全部初值与可能的非终止轨道。',
  '模四余一的奇数一步加速后下降',
  '若奇整数 $n>1$ 且 $n\\equiv1\\pmod4$，则 $U(n)<n$。',
  '$3n+1\\equiv0\\pmod4$，故 $v_2(3n+1)\\ge2$，于是 $U(n)\\le(3n+1)/4<n$，最后一个不等式由 $n>1$ 得到。')]

def seed(tx):
    for order,(pid,title,statement,summary,body,ititle,claim,proof) in enumerate(DATA,1):
        if tx.get('problems',pid): continue
        import time
        p=dict(id=pid,title=title,statement=statement,display_order=order,status='open',lean_target=None,target_status='awaiting_formalization_and_review',created_at=time.time())
        tx.put('problems',p)
        i=dict(id='I'+pid[1:]+'SEED',problem_id=pid,title=ititle,claim=claim,proof=proof,proof_status='claimed_complete',claims_problem_solved=0,gaps=[],references=[],contributor_id='platform_seed',supersedes_id=None,task_id=None,status='received',seed=True,created_at=time.time())
        i['content_hash']=digest(i); tx.put('insights',i)
        task(tx,pid,'insight_explore'); task(tx,pid,'review',i['id'])

def main():
    with Store().transaction() as tx: seed(tx)
    print('Seeded three problems, three insights; repeat-safe.')
if __name__=='__main__': main()
