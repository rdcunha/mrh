from pyscf import lib
from mrh.my_pyscf.grad import mcpdft as mcpdft_grad
from pyscf.df.grad import sacasscf as dfsacasscf_grad
from pyscf.df.grad import rhf as dfrhf_grad
from functools import partial

# I need to resolve the __init__ and get_ham_response members. Otherwise everything should be fine! 
class Gradients (dfsacasscf_grad.Gradients, mcpdft_grad.Gradients):
    
    def __init__(self, pdft, state=None):
        self.auxbasis_response = True
        mcpdft_grad.Gradients.__init__(self, pdft, state=state)

    # TODO: rewrite the partialized fn to take the actual caller, use getattr,
    # and delete this
    def get_ham_response (self, **kwargs):
        pfn = partial (mcpdft_grad.mcpdft_HellmanFeynman_grad,
         auxbasis_response=self.auxbasis_response)
        with lib.temporary_env (mcpdft_grad, mcpdft_HellmanFeynman_grad=pfn):
            return mcpdft_grad.Gradients.get_ham_response (self, **kwargs)

    def kernel (self, **kwargs):
        if not ('mf_grad' in kwargs):
            kwargs['mf_grad'] = dfrhf_grad.Gradients (self.base._scf)
        return mcpdft_grad.Gradients.kernel (self, **kwargs)

    get_wfn_response = mcpdft_grad.Gradients.get_wfn_response
    get_init_guess = mcpdft_grad.Gradients.get_init_guess
    project_Aop = mcpdft_grad.Gradients.project_Aop

