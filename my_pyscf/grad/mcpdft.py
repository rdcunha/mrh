from pyscf.mcscf import newton_casscf, casci, mc1step
from pyscf.grad import rks as rks_grad
from pyscf.dft import gen_grid
from pyscf.lib import logger, pack_tril, current_memory, tag_array
#from mrh.my_pyscf.grad import sacasscf
from pyscf.grad import sacasscf
from pyscf.mcscf.casci import cas_natorb
from mrh.my_pyscf.mcpdft.otpd import get_ontop_pair_density, _grid_ao2mo
from mrh.my_pyscf.mcpdft.pdft_veff import _contract_vot_rho, _contract_ao_vao
from mrh.my_pyscf.mcpdft import _dms
from functools import reduce
from itertools import product
from scipy import linalg
import numpy as np
import time, gc

BLKSIZE = gen_grid.BLKSIZE

def mcpdft_HellmanFeynman_grad (mc, ot, veff1, veff2, mo_coeff=None, ci=None,
        atmlst=None, mf_grad=None, verbose=None, max_memory=None,
        auxbasis_response=False):
    '''Modification of pyscf.grad.casscf.kernel to compute instead the
    Hellman-Feynman gradient terms of MC-PDFT. From the differentiated
    Hamiltonian matrix elements, only the core and Coulomb energy parts
    remain. For the renormalization terms, the effective Fock matrix is
    as in CASSCF, but with the same Hamiltonian substutition that is
    used for the energy response terms. '''
    if mo_coeff is None: mo_coeff = mc.mo_coeff
    if ci is None: ci = mc.ci
    if mf_grad is None: mf_grad = mc._scf.nuc_grad_method()
    if mc.frozen is not None:
        raise NotImplementedError
    if max_memory is None: max_memory = mc.max_memory
    t0 = (logger.process_clock (), logger.perf_counter ())

    mol = mc.mol
    ncore = mc.ncore
    ncas = mc.ncas
    nocc = ncore + ncas
    nelecas = mc.nelecas
    nao, nmo = mo_coeff.shape
    nao_pair = nao * (nao+1) // 2

    mo_occ = mo_coeff[:,:nocc]
    mo_core = mo_coeff[:,:ncore]
    mo_cas = mo_coeff[:,ncore:nocc]

    casdm1, casdm2 = mc.fcisolver.make_rdm12(ci, ncas, nelecas)

# gfock = Generalized Fock, Adv. Chem. Phys., 69, 63
    dm_core = np.dot(mo_core, mo_core.T) * 2
    dm_cas = reduce(np.dot, (mo_cas, casdm1, mo_cas.T))
    # MRH: I need to replace aapa with the equivalent array from veff2
    # I'm not sure how the outcore file-paging system works
    # I also need to generate vhf_c and vhf_a from veff2 rather than the
    # molecule's actual integrals. The true Coulomb repulsion should already be
    # in veff1, but I need to generate the "fake" vj - vk/2 from veff2
    h1e_mo = mo_coeff.T @ (mc.get_hcore() + veff1) @ mo_coeff + veff2.vhf_c
    aapa = np.zeros ((ncas,ncas,nmo,ncas), dtype=h1e_mo.dtype)
    vhf_a = np.zeros ((nmo,nmo), dtype=h1e_mo.dtype)
    for i in range (nmo):
        jbuf = veff2.ppaa[i]
        kbuf = veff2.papa[i]
        aapa[:,:,i,:] = jbuf[ncore:nocc,:,:]
        vhf_a[i] = np.tensordot (jbuf, casdm1, axes=2)
    vhf_a *= 0.5
    # for this potential, vj = vk: vj - vk/2 = vj - vj/2 = vj/2
    gfock = np.zeros ((nmo, nmo))
    gfock[:,:ncore] = (h1e_mo[:,:ncore] + vhf_a[:,:ncore]) * 2
    gfock[:,ncore:nocc] = h1e_mo[:,ncore:nocc] @ casdm1
    gfock[:,ncore:nocc] += np.einsum('uviw,vuwt->it', aapa, casdm2)
    dme0 = reduce(np.dot, (mo_coeff, (gfock+gfock.T)*.5, mo_coeff.T))
    aapa = vhf_a = h1e_mo = gfock = None

    if atmlst is None:
        atmlst = range(mol.natm)
    aoslices = mol.aoslice_by_atom()
    de_hcore = np.zeros ((len(atmlst),3))
    de_renorm = np.zeros ((len(atmlst),3))
    de_coul = np.zeros ((len(atmlst),3))
    de_xc = np.zeros ((len(atmlst),3))
    de_grid = np.zeros ((len(atmlst),3))
    de_wgt = np.zeros ((len(atmlst),3))
    de_aux = np.zeros ((len(atmlst),3))
    de = np.zeros ((len(atmlst),3))

    t0 = logger.timer (mc, 'PDFT HlFn gfock', *t0)
    mo_coeff, ci, mo_occup = cas_natorb (mc, mo_coeff=mo_coeff, ci=ci)
    mo_occ = mo_coeff[:,:nocc]
    mo_core = mo_coeff[:,:ncore]
    mo_cas = mo_coeff[:,ncore:nocc]
    dm1 = dm_core + dm_cas
    dm1 = tag_array (dm1, mo_coeff=mo_coeff, mo_occ=mo_occup)
    # MRH: vhf1c and vhf1a should be the TRUE vj_c and vj_a (no vk!)
    vj = mf_grad.get_jk (dm=dm1)[0]
    hcore_deriv = mf_grad.hcore_generator(mol)
    s1 = mf_grad.get_ovlp(mol)
    if auxbasis_response:
        de_aux += np.squeeze (vj.aux)

    # MRH: Now I have to compute the gradient of the on-top energy
    # This involves derivatives of the orbitals that construct rho and Pi and
    # therefore another set of potentials. It also involves the derivatives of
    # quadrature grid points which propagate through the densities and
    # therefore yet another set of potentials. The orbital-derivative part
    # includes all the grid points and some of the orbitals (- sign); the
    # grid-derivative part includes all of the orbitals and some of the grid
    # points (+ sign). I'll do a loop over grid sections and make arrays of
    # type (3,nao, nao) and (3,nao, ncas, ncas, ncas). I'll contract them
    # within the grid loop for the grid derivatives and in the following
    # orbital loop for the xc derivatives
    # MRH, 05/09/2020: The actual spin density doesn't matter at all in PDFT!
    # I could probably save a fair amount of time by not screwing around with
    # the actual spin density! Also, the cumulant decomposition can always be
    # defined without the spin-density matrices and it's still valid!
    mo_n = mo_occ * mo_occup[None,:nocc]
    casdm1, casdm2 = mc.fcisolver.make_rdm12(ci, ncas, nelecas)
    twoCDM = _dms.dm2_cumulant (casdm2, casdm1)
    dm1s = np.stack ((dm1/2.0,)*2, axis=0)
    dm1 = tag_array (dm1, mo_coeff=mo_occ, mo_occ=mo_occup[:nocc])
    make_rho = ot._numint._gen_rho_evaluator (mol, dm1, 1)[0]
    dvxc = np.zeros ((3,nao))
    idx = np.array ([[1,4,5,6],[2,5,7,8],[3,6,8,9]], dtype=np.int_) 
    # For addressing particular ao derivatives
    if ot.xctype == 'LDA': idx = idx[:,0:1] # For LDAs, no second derivatives
    diag_idx = np.arange(ncas) # for puvx
    diag_idx = diag_idx * (diag_idx+1) // 2 + diag_idx
    casdm2_pack = (twoCDM + twoCDM.transpose (0,1,3,2)).reshape (ncas**2, ncas,
        ncas)
    casdm2_pack = pack_tril (casdm2_pack).reshape (ncas, ncas, -1)
    casdm2_pack[:,:,diag_idx] *= 0.5
    diag_idx = np.arange(ncore, dtype=np.int_) * (ncore + 1) # for pqii
    full_atmlst = -np.ones (mol.natm, dtype=np.int_)
    t1 = logger.timer (mc, 'PDFT HlFn quadrature setup', *t0)
    for k, ia in enumerate (atmlst):
        full_atmlst[ia] = k
    for ia, (coords, w0, w1) in enumerate (rks_grad.grids_response_cc (
            ot.grids)):
        # For the xc potential derivative, I need every grid point in the
        # entire molecule regardless of atmlist. (Because that's about orbs.)
        # For the grid and weight derivatives, I only need the gridpoints that
        # are in atmlst. It is conceivable that I can make this more efficient
        # by only doing cross-combinations of grids and AOs, but I don't know
        # how "mask" works yet or how else I could do this.
        gc.collect ()
        ngrids = coords.shape[0]
        ndao = (1,4)[ot.dens_deriv]
        ndpi = (1,4)[ot.Pi_deriv]
        ncols = 1.05 * 3 * (ndao*(nao+nocc) + max(ndao*nao,ndpi*ncas*ncas))
        remaining_floats = (max_memory - current_memory ()[0]) * 1e6 / 8
        blksize = int (remaining_floats / (ncols*BLKSIZE)) * BLKSIZE
        blksize = max (BLKSIZE, min (blksize, ngrids, BLKSIZE*1200))
        t1 = logger.timer (mc, 'PDFT HlFn quadrature atom {} mask and memory '
            'setup'.format (ia), *t1)
        for ip0 in range (0, ngrids, blksize):
            ip1 = min (ngrids, ip0+blksize)
            mask = gen_grid.make_mask (mol, coords[ip0:ip1])
            logger.info (mc, ('PDFT gradient atom {} slice {}-{} of {} '
                'total').format (ia, ip0, ip1, ngrids))
            ao = ot._numint.eval_ao (mol, coords[ip0:ip1],
                deriv=ot.dens_deriv+1, non0tab=mask) 
            # Need 1st derivs for LDA, 2nd for GGA, etc.
            t1 = logger.timer (mc, ('PDFT HlFn quadrature atom {} ao '
                'grids').format (ia), *t1)
            # Slice down ao so as not to confuse the rho and Pi generators
            if ot.xctype == 'LDA': 
                aoval = ao[0]
            if ot.xctype == 'GGA':
                aoval = ao[:4]
            rho = make_rho (0, aoval, mask, ot.xctype) / 2.0
            rho = np.stack ((rho,)*2, axis=0)
            t1 = logger.timer (mc, ('PDFT HlFn quadrature atom {} rho '
                'calc').format (ia), *t1)
            Pi = get_ontop_pair_density (ot, rho, aoval, twoCDM, mo_cas,
                ot.dens_deriv, mask)
            t1 = logger.timer (mc, ('PDFT HlFn quadrature atom {} Pi '
                'calc').format (ia), *t1)

            # TODO: consistent format requirements for shape of ao grid
            if ot.xctype == 'LDA': 
                aoval = ao[:1]
            moval_occ = _grid_ao2mo (mol, aoval, mo_occ, mask)
            t1 = logger.timer (mc, ('PDFT HlFn quadrature atom {} ao2mo '
                'grid').format (ia), *t1)
            aoval = np.ascontiguousarray ([ao[ix].transpose (0,2,1)
                for ix in idx[:,:ndao]]).transpose (0,1,3,2)
            ao = None
            t1 = logger.timer (mc, ('PDFT HlFn quadrature atom {} ao grid '
                'reshape').format (ia), *t1)
            eot, vot = ot.eval_ot (rho, Pi, weights=w0[ip0:ip1])[:2]
            vrho, vPi = vot
            t1 = logger.timer (mc, ('PDFT HlFn quadrature atom {} '
                'eval_ot').format (ia), *t1)
            puvx_mem = 2 * ndpi * (ip1-ip0) * ncas * ncas * 8 / 1e6
            remaining_mem = max_memory - current_memory ()[0]
            logger.info (mc, ('PDFT gradient memory note: working on {} grid '
                'points; estimated puvx usage = {:.1f} of {:.1f} remaining '
                'MB').format ((ip1-ip0), puvx_mem, remaining_mem))

            # Weight response
            de_wgt += np.tensordot (eot, w1[atmlst,...,ip0:ip1], axes=(0,2))
            t1 = logger.timer (mc, ('PDFT HlFn quadrature atom {} weight '
                'response').format (ia), *t1)

            # Find the atoms that are a part of the atomlist
            # grid correction shouldn't be added if they aren't there
            # The last stuff to vectorize is in get_veff_2body!
            k = full_atmlst[ia]

            # Vpq + Vpqrs * Drs ; I'm not sure why the list comprehension down
            # there doesn't break ao's stride order but I'm not complaining
            vrho = _contract_vot_rho (vPi, rho.sum (0), add_vrho=vrho)
            tmp_dv = np.stack ([ot.get_veff_1body (rho, Pi, [ao_i, moval_occ],
                w0[ip0:ip1], kern=vrho) for ao_i in aoval], axis=0)
            tmp_dv = (tmp_dv * mo_occ[None,:,:] 
                * mo_occup[None,None,:nocc]).sum (2)
            if k >= 0: de_grid[k] += 2 * tmp_dv.sum (1) # Grid response
            dvxc -= tmp_dv # XC response
            vrho = tmp_dv = None
            t1 = logger.timer (mc, ('PDFT HlFn quadrature atom {} Vpq + Vpqrs '
                '* Drs').format (ia), *t1)

            # Vpuvx * Lpuvx ; remember the stupid slowest->fastest->medium
            # stride order of the ao grid arrays
            moval_cas = moval_occ = np.ascontiguousarray (
                moval_occ[...,ncore:].transpose (0,2,1)).transpose (0,2,1)
            tmp_dv = ot.get_veff_2body_kl (rho, Pi, moval_cas, moval_cas,
                w0[ip0:ip1], symm=True, kern=vPi) 
            # tmp_dv.shape = ndpi,ngrids,ncas*(ncas+1)//2
            tmp_dv = np.tensordot (tmp_dv, casdm2_pack, axes=(-1,-1))
            # tmp_dv.shape = ndpi, ngrids, ncas, ncas
            tmp_dv[0] = (tmp_dv[:ndpi] * moval_cas[:ndpi,:,None,:]).sum (0) 
            # Chain and product rule
            tmp_dv[1:ndpi] *= moval_cas[0,:,None,:] 
            # Chain and product rule
            tmp_dv = tmp_dv.sum (-1) 
            # tmp_dv.shape = ndpi, ngrids, ncas
            tmp_dv = np.tensordot (aoval[:,:ndpi], tmp_dv, axes=((1,2),(0,1))) 
            # tmp_dv.shape = comp, nao (orb), ncas (dm2)
            tmp_dv = np.einsum ('cpu,pu->cp', tmp_dv, mo_cas) 
            # tmp_dv.shape = comp, ncas 
            # it's ok to not vectorize this b/c the quadrature grid is gone
            if k >= 0: de_grid[k] += 2 * tmp_dv.sum (1) # Grid response
            dvxc -= tmp_dv # XC response
            tmp_dv = None
            t1 = logger.timer (mc, ('PDFT HlFn quadrature atom {} Vpuvx * '
                'Lpuvx').format (ia), *t1)

            rho = Pi = eot = vot = vPi = aoval = moval_occ = moval_cas = None
            gc.collect ()

    for k, ia in enumerate(atmlst):
        shl0, shl1, p0, p1 = aoslices[ia]
        h1ao = hcore_deriv(ia) # MRH: this should be the TRUE hcore
        de_hcore[k] += np.einsum('xij,ij->x', h1ao, dm1)
        de_renorm[k] -= np.einsum('xij,ij->x', s1[:,p0:p1], dme0[p0:p1]) * 2
        de_coul[k] += np.einsum('xij,ij->x', vj[:,p0:p1], dm1[p0:p1]) * 2
        de_xc[k] += dvxc[:,p0:p1].sum (1) * 2 # All grids; only some orbitals

    de_nuc = mf_grad.grad_nuc(mol, atmlst)

    logger.debug (mc, "MC-PDFT Hellmann-Feynman nuclear :\n{}".format (de_nuc))
    logger.debug (mc, "MC-PDFT Hellmann-Feynman hcore component:\n{}".format (
        de_hcore))
    logger.debug (mc, "MC-PDFT Hellmann-Feynman coulomb component:\n{}".format
        (de_coul))
    logger.debug (mc, "MC-PDFT Hellmann-Feynman xc component:\n{}".format (
        de_xc))
    logger.debug (mc, ("MC-PDFT Hellmann-Feynman quadrature point component:"
        "\n{}").format (de_grid))
    logger.debug (mc, ("MC-PDFT Hellmann-Feynman quadrature weight component:"
        "\n{}").format (de_wgt))
    logger.debug (mc, "MC-PDFT Hellmann-Feynman renorm component:\n{}".format (
        de_renorm))

    de = de_nuc + de_hcore + de_coul + de_renorm + de_xc + de_grid + de_wgt


    if auxbasis_response:
        de += de_aux
        logger.debug (mc, "MC-PDFT Hellmann-Feynman aux component:\n{}".format
            (de_aux))

    t1 = logger.timer (mc, 'PDFT HlFn total', *t0)

    return de

# TODO: docstrings (parent classes???)
# TODO: add a consistent threshold for elimination of degenerate-state rotations
class Gradients (sacasscf.Gradients):

    def __init__(self, pdft, state=None):
        super().__init__(pdft, state=state)
        # TODO: gradient of PDFT state-average energy 
        # i.e., state = 0 & nroots > 1 case
        if self.state is None and self.nroots == 1:
            self.state = 0
        self.e_mcscf = self.base.e_mcscf
        self._not_implemented_check ()

    def _not_implemented_check (self):
        name = self.__class__.__name__
        if (isinstance (self.base, casci.CASCI) and not
            isinstance (self.base, mc1step.CASSCF)):
            raise NotImplementedError (
                "{} for CASCI-based MC-PDFT".format (name)
                )
        ot, otxc, nelecas = self.base.otfnal, self.base.otxc, self.base.nelecas
        spin = abs (nelecas[0]-nelecas[1])
        omega, alpha, hyb = ot._numint.rsh_and_hybrid_coeff (
            otxc, spin=spin)
        hyb_x, hyb_c = hyb
        if hyb_x or hyb_c:
            raise NotImplementedError (
                "{} for hybrid MC-PDFT functionals".format (name)
                )
        if omega or alpha:
            raise NotImplementedError (
                "{} for range-separated MC-PDFT functionals".format (name)
                )

    def get_wfn_response (self, atmlst=None, state=None, verbose=None, mo=None,
            ci=None, veff1=None, veff2=None, nlag=None, **kwargs):
        if state is None: state = self.state
        if atmlst is None: atmlst = self.atmlst
        if verbose is None: verbose = self.verbose
        if mo is None: mo = self.base.mo_coeff
        if ci is None: ci = self.base.ci
        if nlag is None: nlag = self.nlag
        if (veff1 is None) or (veff2 is None):
            veff1, veff2 = self.base.get_pdft_veff (mo, ci[state],
                incl_coul=True, paaa_only=True)
        sing_tol = getattr (self, 'sing_tol_sasa', 1e-8)
        ndet = ci[state].size
        fcasscf = self.make_fcasscf (state)
        fcasscf.mo_coeff = mo
        fcasscf.ci = ci[state]
        def my_hcore ():
            return self.base.get_hcore () + veff1
        fcasscf.get_hcore = my_hcore

        g_all_state = newton_casscf.gen_g_hop (fcasscf, mo, ci[state], veff2,
            verbose)[0]

        g_all = np.zeros (nlag)
        g_all[:self.ngorb] = g_all_state[:self.ngorb]
        # Eliminate gradient of self-rotation and rotation into
        # degenerate states
        spin_states = np.asarray (self.spin_states)
        idx_spin = spin_states==spin_states[state]
        e_gap = self.e_mcscf-self.e_mcscf[state] if self.nroots>1 else [0.0]
        idx_degen = np.abs (e_gap)<sing_tol
        idx = np.where (idx_spin & idx_degen)[0]
        assert (state in idx)
        gci_state = g_all_state[self.ngorb:]
        ci_proj = np.asarray ([ci[i].ravel () for i in idx])
        gci_sa = np.dot (ci_proj, gci_state)
        gci_state -= np.dot (gci_sa, ci_proj)
        gci = g_all[self.ngorb:]
        offs = 0
        if state>0:
            offs = sum ([na * nb for na, nb in zip(
                        self.na_states[:state], self.nb_states[:state])]) 
        ndet = self.na_states[state]*self.nb_states[state]
        gci[offs:][:ndet] += gci_state 
        return g_all

    def get_ham_response (self, state=None, atmlst=None, verbose=None, mo=None,
            ci=None, eris=None, mf_grad=None, veff1=None, veff2=None,
            **kwargs):
        if state is None: state = self.state
        if atmlst is None: atmlst = self.atmlst
        if verbose is None: verbose = self.verbose
        if mo is None: mo = self.base.mo_coeff
        if ci is None: ci = self.base.ci
        if (veff1 is None) or (veff2 is None):
            assert (False), kwargs
            veff1, veff2 = self.base.get_pdft_veff (mo, ci[state],
                incl_coul=True, paaa_only=True)
        fcasscf = self.make_fcasscf (state)
        fcasscf.mo_coeff = mo
        fcasscf.ci = ci[state]
        return mcpdft_HellmanFeynman_grad (fcasscf, self.base.otfnal, veff1,
            veff2, mo_coeff=mo, ci=ci[state], atmlst=atmlst, mf_grad=mf_grad,
            verbose=verbose)

    def get_init_guess (self, bvec, Adiag, Aop, precond):
        '''Initial guess should solve the problem for SA-SA rotations'''
        sing_tol = getattr (self, 'sing_tol_sasa', 1e-8)
        ci = self.base.ci
        state = self.state
        if self.nroots == 1: ci = [ci,]
        idx_spin = [i for i in range (self.nroots)
                    if self.spin_states[i]==self.spin_states[state]]
        nroots_blk = len (idx_spin)
        ci_blk = np.asarray ([ci[i].ravel () for i in idx_spin])
        ndet = ci_blk.shape[-1]
        b_orb, b_ci = self.unpack_uniq_var (bvec)
        b_ci_blk = np.asarray ([b_ci[i].ravel () for i in idx_spin])
        x0 = np.zeros_like (bvec)
        if self.nroots > 1:
            b_sa = np.dot (ci_blk.conjugate (), b_ci[state].ravel ())
            A_sa = 2 * self.weights[state] * (self.e_mcscf 
                - self.e_mcscf[state])
            idx_null = np.abs (A_sa)<sing_tol
            assert (idx_null[state])
            A_sa = A_sa[idx_spin]
            idx_null = np.abs (A_sa)<sing_tol
            if np.any (np.abs (b_sa[idx_null])>=sing_tol):
                logger.warn (self, 'Singular Hessian in CP-MCPDFT!')
            idx_null &= np.abs (b_sa)<sing_tol
            A_sa[idx_null] = sing_tol
            x0_sa = -b_sa / A_sa # Hessian is diagonal so: easy
            ovlp = ci_blk.conjugate () @ b_ci_blk.T
            logger.debug (self, 'Linear response SA-SA part:\n{}'.format (
                ovlp))
            logger.debug (self, 'Linear response SA-CI norms:\n{}'.format (
                linalg.norm (b_ci_blk.T - ci_blk.T @ ovlp, axis=1)))
            if self.ngorb: logger.debug (self, 'Linear response orbital '
                'norms:\n{}'.format (linalg.norm (bvec[:self.ngorb])))
            logger.debug (self, 'SA-SA Lagrange multiplier for root '
                '{}:\n{}'.format (state, x0_sa))
            x0_orb, x0_ci = self.unpack_uniq_var (x0)
            x0_ci[state] = np.dot (x0_sa, ci_blk).reshape (
                self.na_states[state], self.nb_states[state])
            x0 = self.pack_uniq_var (x0_orb, x0_ci)
        r0 = bvec + Aop (x0)
        r0_orb, r0_ci = self.unpack_uniq_var (r0)
        r0_ci_blk = np.asarray ([r0_ci[i].ravel () for i in idx_spin])
        ovlp = ci_blk.conjugate () @ r0_ci_blk.T
        logger.debug (self, 'Lagrange residual SA-SA part after solving SA-SA'
            ' part:\n{}'.format (ovlp))
        logger.debug (self, 'Lagrange residual SA-CI norms after solving SA-SA'
            ' part:\n{}'.format (linalg.norm (r0_ci_blk.T - ci_blk.T @ ovlp,
            axis=1)))
        if self.ngorb: logger.debug (self, 'Lagrange residual orbital norms '
            'after solving SA-SA part:\n{}'.format (linalg.norm (
            r0_orb)))
        x0 += precond (-r0)
        r1 = bvec + Aop (x0)
        r1_orb, r1_ci = self.unpack_uniq_var (r0)
        r1_ci_blk = np.asarray ([r1_ci[i].ravel () for i in idx_spin])
        ovlp = ci_blk.conjugate () @ r1_ci_blk.T
        logger.debug (self, 'Lagrange residual SA-SA part after first '
            'precondition:\n{}'.format (ovlp))
        logger.debug (self, 'Lagrange residual SA-CI norms after first '
            'precondition:\n{}'.format (linalg.norm (r1_ci_blk.T - ci_blk.T @ ovlp,
            axis=1)))
        if self.ngorb: logger.debug (self, 'Lagrange residual orbital norms '
            'after first precondition:\n{}'.format (linalg.norm (
            r1_orb)))
        return x0

    def kernel (self, **kwargs):
        '''Cache the effective Hamiltonian terms so you don't have to
        calculate them twice
        '''
        state = kwargs['state'] if 'state' in kwargs else self.state
        if state is None:
            raise NotImplementedError ('Gradient of PDFT state-average energy')
        self.state = state # Not the best code hygiene maybe
        mo = kwargs['mo'] if 'mo' in kwargs else self.base.mo_coeff
        ci = kwargs['ci'] if 'ci' in kwargs else self.base.ci
        if isinstance (ci, np.ndarray): ci = [ci] # hack hack hack...
        kwargs['ci'] = ci
        if ('veff1' not in kwargs) or ('veff2' not in kwargs):
            kwargs['veff1'], kwargs['veff2'] = self.base.get_pdft_veff (mo,
                ci, incl_coul=True, paaa_only=True, state=state)
        return super().kernel (**kwargs)

    def project_Aop (self, Aop, ci, state):
        '''Wrap the Aop function to project out redundant degrees of
        freedom for the CI part.  What's redundant changes between
        SA-CASSCF and MC-PDFT so modify this part in child classes.
        '''
        weights, e_mcscf = np.asarray (self.weights), np.asarray (self.e_mcscf)
        try:
            A_sa = 2 * weights[state] * (e_mcscf - e_mcscf[state])
        except IndexError as e:
            assert (self.nroots == 1), e
            A_sa = 0
        except Exception as e:
            print (self.weights, self.e_mcscf)
            raise (e)
        idx_spin = [i for i in range (self.nroots)
                    if self.spin_states[i]==self.spin_states[state]]
        if self.nroots==1:
            ci_blk = ci.reshape (1, -1)
            ci = [ci]
        else:
            ci_blk = np.asarray ([ci[i].ravel () for i in idx_spin])
            A_sa = A_sa[idx_spin]
        def my_Aop (x):
            Ax = Aop (x)
            x_orb, x_ci = self.unpack_uniq_var (x)
            Ax_orb, Ax_ci = self.unpack_uniq_var (Ax)
            for i, j in product (range (self.nroots), repeat=2):
                if self.spin_states[i] != self.spin_states[j]: continue
                Ax_ci[i] -= np.dot (Ax_ci[i].ravel (), ci[j].ravel ()) * ci[j]
            # Add back in the SA rotation part but from the true energy conds
            x_sa = np.dot (ci_blk.conjugate (), x_ci[state].ravel ())
            Ax_ci[state] += np.dot (x_sa * A_sa, ci_blk).reshape (
                Ax_ci[state].shape)
            Ax = self.pack_uniq_var (Ax_orb, Ax_ci)
            return Ax
        return my_Aop
