from pyscf.scf.rohf import get_roothaan_fock
from pyscf.fci import cistring
from pyscf.mcscf import casci, casci_symm, df
from pyscf import symm, gto, scf, ao2mo, lib
from mrh.my_pyscf.mcscf.addons import state_average_n_mix, get_h1e_zipped_fcisolver, las2cas_civec
from mrh.my_pyscf.mcscf import lasci_sync, _DFLASCI
from mrh.my_pyscf.fci import csf_solver
from mrh.my_pyscf.df.sparse_df import sparsedf_array
from mrh.my_pyscf.mcscf.lassi import lassi
from mrh.my_pyscf.mcscf.productstate import ProductStateFCISolver
from itertools import combinations
from scipy.sparse import linalg as sparse_linalg
from scipy import linalg
import numpy as np
import copy

def LASCI (mf_or_mol, ncas_sub, nelecas_sub, **kwargs):
    if isinstance(mf_or_mol, gto.Mole):
        mf = scf.RHF(mf_or_mol)
    else:
        mf = mf_or_mol
    if mf.mol.symmetry: 
        las = LASCISymm (mf, ncas_sub, nelecas_sub, **kwargs)
    else:
        las = LASCINoSymm (mf, ncas_sub, nelecas_sub, **kwargs)
    if getattr (mf, 'with_df', None):
        las = density_fit (las, with_df = mf.with_df) 
    return las

def get_grad (las, mo_coeff=None, ci=None, ugg=None, h1eff_sub=None, h2eff_sub=None,
              veff=None, dm1s=None):
    '''Return energy gradient for orbital rotation and CI relaxation.

    Args:
        las : instance of :class:`LASCINoSymm`

    Kwargs:
        mo_coeff : ndarray of shape (nao,nmo)
            Contains molecular orbitals
        ci : list (length=nfrags) of list (length=nroots) of ndarray
            Contains CI vectors
        ugg : instance of :class:`LASCI_UnitaryGroupGenerators`
        h1eff_sub : list (length=nfrags) of list (length=nroots) of ndarray
            Contains effective one-electron Hamiltonians experienced by each fragment
            in each state
        h2eff_sub : ndarray of shape (nmo,ncas**2*(ncas+1)/2)
            Contains ERIs (p1a1|a2a3), lower-triangular in the a2a3 indices
        veff : ndarray of shape (2,nao,nao)
            Spin-separated, state-averaged 1-electron mean-field potential in the AO basis
        dm1s : ndarray of shape (2,nao,nao)
            Spin-separated, state-averaged 1-RDM in the AO basis

    Returns:
        gorb : ndarray of shape (ugg.nvar_orb,)
            Orbital rotation gradients as a flat array
        gci : ndarray of shape (sum(ugg.ncsf_sub),)
            CI relaxation gradients as a flat array
        gx : ndarray
            Orbital rotation gradients for temporarily frozen orbitals in the "LASCI" problem
    '''
    if mo_coeff is None: mo_coeff = las.mo_coeff
    if ci is None: ci = las.ci
    if ugg is None: ugg = las.get_ugg (mo_coeff, ci)
    if dm1s is None: dm1s = las.make_rdm1s (mo_coeff=mo_coeff, ci=ci)
    if h2eff_sub is None: h2eff_sub = las.get_h2eff (mo_coeff)
    if veff is None:
        veff = las.get_veff (dm1s = dm1s.sum (0))
        veff = las.split_veff (veff, h2eff_sub, mo_coeff=mo_coeff, ci=ci)
    if h1eff_sub is None: h1eff_sub = las.get_h1eff (mo_coeff, ci=ci, veff=veff,
                                                     h2eff_sub=h2eff_sub)

    gorb = get_grad_orb (las, mo_coeff=mo_coeff, ci=ci, h2eff_sub=h2eff_sub, veff=veff, dm1s=dm1s)
    gci = get_grad_ci (las, mo_coeff=mo_coeff, ci=ci, h1eff_sub=h1eff_sub, h2eff_sub=h2eff_sub,
                       veff=veff)

    idx = ugg.get_gx_idx ()
    gx = gorb[idx]
    gint = ugg.pack (gorb, gci)
    gorb = gint[:ugg.nvar_orb]
    gci = gint[ugg.nvar_orb:]
    return gorb, gci, gx.ravel ()

def get_grad_orb (las, mo_coeff=None, ci=None, h2eff_sub=None, veff=None, dm1s=None, hermi=-1):
    '''Return energy gradient for orbital rotation.

    Args:
        las : instance of :class:`LASCINoSymm`

    Kwargs:
        mo_coeff : ndarray of shape (nao,nmo)
            Contains molecular orbitals
        ci : list (length=nfrags) of list (length=nroots) of ndarray
            Contains CI vectors
        h2eff_sub : ndarray of shape (nmo,ncas**2*(ncas+1)/2)
            Contains ERIs (p1a1|a2a3), lower-triangular in the a2a3 indices
        veff : ndarray of shape (2,nao,nao)
            Spin-separated, state-averaged 1-electron mean-field potential in the AO basis
        dm1s : ndarray of shape (2,nao,nao)
            Spin-separated, state-averaged 1-RDM in the AO basis
        hermi : integer
            Control (anti-)symmetrization. 0 means to return the effective Fock matrix,
            F1 = h.D + g.d. -1 means to return the true orbital-rotation gradient, which is skew-
            symmetric: gorb = F1 - F1.T. +1 means to return the symmetrized effective Fock matrix,
            (F1 + F1.T) / 2. The factor of 2 difference between hermi=-1 and the other two options
            is intentional and necessary.

    Returns:
        gorb : ndarray of shape (nmo,nmo)
            Orbital rotation gradients as a square antihermitian array
    '''
    if mo_coeff is None: mo_coeff = las.mo_coeff
    if ci is None: ci = las.ci
    if dm1s is None: dm1s = las.make_rdm1s (mo_coeff=mo_coeff, ci=ci)
    if h2eff_sub is None: h2eff_sub = las.get_h2eff (mo_coeff)
    if veff is None:
        veff = las.get_veff (dm1s = dm1s.sum (0))
        veff = las.split_veff (veff, h2eff_sub, mo_coeff=mo_coeff, ci=ci)
    nao, nmo = mo_coeff.shape
    ncore = las.ncore
    ncas = las.ncas
    nocc = las.ncore + las.ncas
    smo_cas = las._scf.get_ovlp () @ mo_coeff[:,ncore:nocc]
    smoH_cas = smo_cas.conj ().T

    # The orbrot part
    h1s = las.get_hcore ()[None,:,:] + veff
    f1 = h1s[0] @ dm1s[0] + h1s[1] @ dm1s[1]
    f1 = mo_coeff.conjugate ().T @ f1 @ las._scf.get_ovlp () @ mo_coeff
    # ^ I need the ovlp there to get dm1s back into its correct basis
    casdm2 = las.make_casdm2 (ci=ci)
    casdm1s = np.stack ([smoH_cas @ d @ smo_cas for d in dm1s], axis=0)
    casdm1 = casdm1s.sum (0)
    casdm2 -= np.multiply.outer (casdm1, casdm1)
    casdm2 += np.multiply.outer (casdm1s[0], casdm1s[0]).transpose (0,3,2,1)
    casdm2 += np.multiply.outer (casdm1s[1], casdm1s[1]).transpose (0,3,2,1)
    eri = h2eff_sub.reshape (nmo*ncas, ncas*(ncas+1)//2)
    eri = lib.numpy_helper.unpack_tril (eri).reshape (nmo, ncas, ncas, ncas)
    f1[:,ncore:nocc] += np.tensordot (eri, casdm2, axes=((1,2,3),(1,2,3)))

    if hermi == -1:
        return f1 - f1.T
    elif hermi == 1:
        return .5*(f1+f1.T)
    elif hermi == 0:
        return f1
    else:
        raise ValueError ("kwarg 'hermi' must = -1, 0, or +1")

def get_grad_ci (las, mo_coeff=None, ci=None, h1eff_sub=None, h2eff_sub=None, veff=None):
    '''Return energy gradient for CI relaxation.

    Args:
        las : instance of :class:`LASCINoSymm`

    Kwargs:
        mo_coeff : ndarray of shape (nao,nmo)
            Contains molecular orbitals
        ci : list (length=nfrags) of list (length=nroots) of ndarray
            Contains CI vectors
        h1eff_sub : list (length=nfrags) of list (length=nroots) of ndarray
            Contains effective one-electron Hamiltonians experienced by each fragment
            in each state
        h2eff_sub : ndarray of shape (nmo,ncas**2*(ncas+1)/2)
            Contains ERIs (p1a1|a2a3), lower-triangular in the a2a3 indices
        veff : ndarray of shape (2,nao,nao)
            Spin-separated, state-averaged 1-electron mean-field potential in the AO basis

    Returns:
        gci : list (length=nfrags) of list (length=nroots) of ndarray
            CI relaxation gradients in the shape of CI vectors
    '''
    if mo_coeff is None: mo_coeff = las.mo_coeff
    if ci is None: ci = las.ci
    if h2eff_sub is None: h2eff_sub = las.get_h2eff (mo_coeff)
    if h1eff_sub is None: h1eff_sub = las.get_h1eff (mo_coeff, ci=ci, veff=veff,
                                                     h2eff_sub=h2eff_sub)
    gci = []
    for isub, (fcibox, h1e, ci0, ncas, nelecas) in enumerate (zip (
            las.fciboxes, h1eff_sub, ci, las.ncas_sub, las.nelecas_sub)):
        eri_cas = las.get_h2eff_slice (h2eff_sub, isub, compact=8)
        max_memory = max(400, las.max_memory-lib.current_memory()[0])
        linkstrl = fcibox.states_gen_linkstr (ncas, nelecas, True)
        linkstr  = fcibox.states_gen_linkstr (ncas, nelecas, False)
        h2eff = fcibox.states_absorb_h1e(h1e, eri_cas, ncas, nelecas, .5)
        hc0 = fcibox.states_contract_2e(h2eff, ci0, ncas, nelecas, link_index=linkstrl)
        hc0 = [hc.ravel () for hc in hc0]
        ci0 = [c.ravel () for c in ci0]
        gci.append ([2.0 * (hc - c * (c.dot (hc))) for c, hc in zip (ci0, hc0)])
    return gci

def density_fit (las, auxbasis=None, with_df=None):
    ''' Here I ONLY need to attach the tag and the df object because I put conditionals in
        LASCINoSymm to make my life easier '''
    las_class = las.__class__
    if with_df is None:
        if (getattr(las._scf, 'with_df', None) and
            (auxbasis is None or auxbasis == las._scf.with_df.auxbasis)):
            with_df = las._scf.with_df
        else:
            with_df = df.DF(las.mol)
            with_df.max_memory = las.max_memory
            with_df.stdout = las.stdout
            with_df.verbose = las.verbose
            with_df.auxbasis = auxbasis
    class DFLASCI (las_class, _DFLASCI):
        def __init__(self, my_las):
            self.__dict__.update(my_las.__dict__)
            #self.grad_update_dep = 0
            self.with_df = with_df
            self._keys = self._keys.union(['with_df'])
    return DFLASCI (las)

def h1e_for_cas (las, mo_coeff=None, ncas=None, ncore=None, nelecas=None, ci=None, ncas_sub=None,
                 nelecas_sub=None, veff=None, h2eff_sub=None, casdm1s_sub=None, casdm1frs=None):
    ''' Effective one-body Hamiltonians (plural) for a LASCI problem

    Args:
        las: a LASCI object

    Kwargs:
        mo_coeff: ndarray of shape (nao,nmo)
            Orbital coefficients ordered on the columns as: 
            core orbitals, subspace 1, subspace 2, ..., external orbitals
        ncas: integer
            As in PySCF's existing CASCI/CASSCF implementation
        nelecas: sequence of 2 integers
            As in PySCF's existing CASCI/CASSCF implementation
        ci: list (length=nfrags) of list (length=nroots) of ndarrays
            Contains CI vectors
        ncas_sub: ndarray of shape (nsub)
            Number of active orbitals in each subspace
        nelecas_sub: ndarray of shape (nsub,2)
            na, nb in each subspace
        veff: ndarray of shape (2, nao, nao)
            Contains spin-separated, state-averaged effective potential
        h2eff_sub : ndarray of shape (nmo,ncas**2*(ncas+1)/2)
            Contains ERIs (p1a1|a2a3), lower-triangular in the a2a3 indices
        casdm1s_sub : list (length=nfrags) of ndarrays
            Contains state-averaged, spin-separated 1-RDMs in the localized active subspaces
        casdm1frs : list (length=nfrags) of list (length=nroots) of ndarrays
            Contains spin-separated 1-RDMs for each state in the localized active subspaces

    Returns:
        h1e_fr: list (length=nfrags) of list (length=nroots) of ndarrays
            Spin-separated 1-body Hamiltonian operator for each fragment and state
    '''
    if mo_coeff is None: mo_coeff = las.mo_coeff
    if ncas is None: ncas = las.ncas
    if ncore is None: ncore = las.ncore
    if ncas_sub is None: ncas_sub = las.ncas_sub
    if nelecas_sub is None: nelecas_sub = las.nelecas_sub
    if ncore is None: ncore = las.ncore
    if ci is None: ci = las.ci
    if h2eff_sub is None: h2eff_sub = las.get_h2eff (mo_coeff)
    if casdm1frs is None: casdm1frs = las.states_make_casdm1s_sub (ci=ci)
    if casdm1s_sub is None: casdm1s_sub = [np.einsum ('rsij,r->sij',dm,las.weights)
                                           for dm in casdm1frs]
    if veff is None:
        veff = las.get_veff (dm1s = las.make_rdm1 (mo_coeff=mo_coeff, ci=ci))
        veff = las.split_veff (veff, h2eff_sub, mo_coeff=mo_coeff, ci=ci, casdm1s_sub=casdm1s_sub)

    # First pass: split by root  
    nocc = ncore + ncas
    nmo = mo_coeff.shape[-1]
    mo_cas = mo_coeff[:,ncore:nocc]
    moH_cas = mo_cas.conj ().T 
    h1e = moH_cas @ (las.get_hcore ()[None,:,:] + veff) @ mo_cas
    h1e_r = np.empty ((las.nroots, 2, ncas, ncas), dtype=h1e.dtype)
    h2e = lib.numpy_helper.unpack_tril (h2eff_sub.reshape (nmo*ncas,
        ncas*(ncas+1)//2)).reshape (nmo, ncas, ncas, ncas)[ncore:nocc,:,:,:]
    avgdm1s = np.stack ([linalg.block_diag (*[dm[spin] for dm in casdm1s_sub])
                         for spin in range (2)], axis=0)
    for state in range (las.nroots):
        statedm1s = np.stack ([linalg.block_diag (*[dm[state][spin] for dm in casdm1frs])
                               for spin in range (2)], axis=0)
        dm1s = statedm1s - avgdm1s 
        j = np.tensordot (dm1s, h2e, axes=((1,2),(2,3)))
        k = np.tensordot (dm1s, h2e, axes=((1,2),(2,1)))
        h1e_r[state] = h1e + j + j[::-1] - k


    # Second pass: split by fragment and subtract double-counting
    h1e_fr = []
    for ix, casdm1s_r in enumerate (casdm1frs):
        p = sum (las.ncas_sub[:ix])
        q = p + las.ncas_sub[ix]
        h1e = h1e_r[:,:,p:q,p:q]
        h2e = las.get_h2eff_slice (h2eff_sub, ix)
        j = np.tensordot (casdm1s_r, h2e, axes=((2,3),(2,3)))
        k = np.tensordot (casdm1s_r, h2e, axes=((2,3),(2,1)))
        h1e_fr.append (h1e - j - j[:,::-1] + k)

    return h1e_fr

def get_fock (las, mo_coeff=None, ci=None, eris=None, casdm1s=None, verbose=None, veff=None,
              dm1s=None):
    ''' f_pq = h_pq + (g_pqrs - g_psrq/2) D_rs, AO basis
    Note the difference between this and h1e_for_cas: h1e_for_cas only has
    JK terms from electrons outside the "current" active subspace; get_fock
    includes JK from all electrons. This is also NOT the "generalized Fock matrix"
    of orbital gradients (but it can be used in calculating those if you do a
    semi-cumulant decomposition).
    The "eris" kwarg does not do anything and is retained only for backwards
    compatibility (also why I don't just call las.make_rdm1) '''
    if mo_coeff is None: mo_coeff = las.mo_coeff
    if ci is None: ci = las.ci
    if casdm1s is None: casdm1s = las.make_casdm1s (ci=ci)
    if dm1s is None:
        mo_cas = mo_coeff[:,las.ncore:][:,:las.ncas]
        moH_cas = mo_cas.conjugate ().T
        mo_core = mo_coeff[:,:las.ncore]
        moH_core = mo_core.conjugate ().T
        dm1s = [(mo_core @ moH_core) + (mo_cas @ d @ moH_cas) for d in list(casdm1s)]
    if veff is not None:
        fock = las.get_hcore()[None,:,:] + veff
        return get_roothaan_fock (fock, dm1s, las._scf.get_ovlp ())
    dm1 = dm1s[0] + dm1s[1]
    if isinstance (las, _DFLASCI):
        vj, vk = las.with_df.get_jk(dm1, hermi=1)
    else:
        vj, vk = las._scf.get_jk(las.mol, dm1, hermi=1)
    fock = las.get_hcore () + vj - (vk/2)
    return fock

def canonicalize (las, mo_coeff=None, ci=None, casdm1fs=None, natorb_casdm1=None, veff=None,
                  h2eff_sub=None, orbsym=None):
    if mo_coeff is None: mo_coeff = las.mo_coeff
    if ci is None: ci = las.ci
    if casdm1fs is None: casdm1fs = las.make_casdm1s_sub (ci=ci)

    # In-place safety
    mo_coeff = mo_coeff.copy ()
    ci = copy.deepcopy (ci)

    nao, nmo = mo_coeff.shape
    ncore = las.ncore
    nocc = ncore + las.ncas
    ncas_sub = las.ncas_sub
    nelecas_sub = las.nelecas_sub

    # Passing casdm1 or lasdm1 only affects the canonicalization of the active orbitals
    umat = np.zeros_like (mo_coeff)
    casdm1s = np.stack ([linalg.block_diag (*[dm[0] for dm in casdm1fs]),
                         linalg.block_diag (*[dm[1] for dm in casdm1fs])], axis=0)
    fock = mo_coeff.conjugate ().T @ las.get_fock (mo_coeff=mo_coeff, casdm1s=casdm1s, veff=veff)
    fock = fock @ mo_coeff
    if natorb_casdm1 is None: # State-average natural orbitals by default
        natorb_casdm1 = casdm1s.sum (0)

    # Inactive-inactive
    orbsym_i = None if orbsym is None else orbsym[:ncore]
    fock_i = fock[:ncore,:ncore]
    if ncore:
        ene, umat[:ncore,:ncore] = las._eig (fock_i, 0, 0, orbsym_i)
        idx = np.argsort (ene)
        umat[:ncore,:ncore] = umat[:ncore,:ncore][:,idx]
        if orbsym_i is not None: orbsym[:ncore] = orbsym[:ncore][idx]
    # Active-active
    check_diag = natorb_casdm1.copy ()
    for ix, ncas in enumerate (ncas_sub):
        i = sum (ncas_sub[:ix])
        j = i + ncas
        check_diag[i:j,i:j] = 0.0
    if np.amax (np.abs (check_diag)) < 1e-8:
        # No off-diagonal RDM elements -> extra effort to prevent diagonalizer from breaking frags
        for isub, (ncas, nelecas) in enumerate (zip (ncas_sub, nelecas_sub)):
            i = sum (ncas_sub[:isub])
            j = i + ncas
            dm1 = natorb_casdm1[i:j,i:j]
            i += ncore
            j += ncore
            orbsym_i = None if orbsym is None else orbsym[i:j]
            occ, umat[i:j,i:j] = las._eig (dm1, 0, 0, orbsym_i)
            idx = np.argsort (occ)[::-1]
            umat[i:j,i:j] = umat[i:j,i:j][:,idx]
            if orbsym_i is not None: orbsym[i:j] = orbsym[i:j][idx]
            if ci is not None:
                fcibox = las.fciboxes[isub]
                ci[isub] = fcibox.states_transform_ci_for_orbital_rotation (
                    ci[isub], ncas, nelecas, umat[i:j,i:j])
    else: # You can't get proper LAS-type CI vectors w/out active space fragmentation
        ci = None 
        orbsym_cas = None if orbsym is None else orbsym[ncore:nocc]
        occ, umat[ncore:nocc,ncore:nocc] = las._eig (natorb_casdm1, 0, 0, orbsym_cas)
        idx = np.argsort (occ)[::-1]
        umat[ncore:nocc,ncore:nocc] = umat[ncore:nocc,ncore:nocc][:,idx]
        if orbsym_cas is not None: orbsym[ncore:nocc] = orbsym[ncore:nocc][idx]
    # External-external
    if nmo-nocc:
        orbsym_i = None if orbsym is None else orbsym[nocc:]
        fock_i = fock[nocc:,nocc:]
        ene, umat[nocc:,nocc:] = las._eig (fock_i, 0, 0, orbsym_i)
        idx = np.argsort (ene)
        umat[nocc:,nocc:] = umat[nocc:,nocc:][:,idx]
        if orbsym_i is not None: orbsym[nocc:] = orbsym[nocc:][idx]

    # Final
    mo_occ = np.zeros (nmo, dtype=natorb_casdm1.dtype)
    if ncore: mo_occ[:ncore] = 2
    ucas = umat[ncore:nocc,ncore:nocc]
    mo_occ[ncore:nocc] = ((natorb_casdm1 @ ucas) * ucas).sum (0)
    mo_ene = ((fock @ umat) * umat.conjugate ()).sum (0)
    mo_ene[ncore:][:sum (ncas_sub)] = 0.0
    mo_coeff = mo_coeff @ umat
    if orbsym is not None:
        '''
        print ("This is the second call to label_orb_symm inside of canonicalize") 
        orbsym = symm.label_orb_symm (las.mol, las.mol.irrep_id,
                                      las.mol.symm_orb, mo_coeff,
                                      s=las._scf.get_ovlp ())
        #mo_coeff = las.label_symmetry_(mo_coeff)
        '''
        mo_coeff = lib.tag_array (mo_coeff, orbsym=orbsym)
    if h2eff_sub is not None:
        h2eff_sub = lib.numpy_helper.unpack_tril (h2eff_sub.reshape (nmo*las.ncas, -1))
        h2eff_sub = h2eff_sub.reshape (nmo, las.ncas, las.ncas, las.ncas)
        h2eff_sub = np.tensordot (umat, h2eff_sub, axes=((0),(0)))
        h2eff_sub = np.tensordot (ucas, h2eff_sub, axes=((0),(1))).transpose (1,0,2,3)
        h2eff_sub = np.tensordot (ucas, h2eff_sub, axes=((0),(2))).transpose (1,2,0,3)
        h2eff_sub = np.tensordot (ucas, h2eff_sub, axes=((0),(3))).transpose (1,2,3,0)
        h2eff_sub = h2eff_sub.reshape (nmo*las.ncas, las.ncas, las.ncas)
        h2eff_sub = lib.numpy_helper.pack_tril (h2eff_sub).reshape (nmo, -1)
    return mo_coeff, mo_ene, mo_occ, ci, h2eff_sub

def get_init_guess_ci (las, mo_coeff=None, h2eff_sub=None, ci0=None):
    # TODO: come up with a better algorithm? This might be working better than what I had before
    # but it omits inter-active Coulomb and exchange interactions altogether. Is there a
    # non-outer-product algorithm for finding the lowest-energy single product of CSFs?
    if mo_coeff is None: mo_coeff = las.mo_coeff
    if ci0 is None: ci0 = [[None for i in range (las.nroots)] for j in range (las.nfrags)]
    if h2eff_sub is None: h2eff_sub = las.get_h2eff (mo_coeff)
    nmo = mo_coeff.shape[-1]
    ncore, ncas = las.ncore, las.ncas
    nocc = ncore + ncas
    dm1_core= 2 * mo_coeff[:,:ncore] @ mo_coeff[:,:ncore].conj ().T
    h1e_ao = las._scf.get_fock (dm=dm1_core)
    eri_cas = lib.numpy_helper.unpack_tril (h2eff_sub.reshape (nmo*ncas, ncas*(ncas+1)//2))
    eri_cas = eri_cas.reshape (nmo, ncas, ncas, ncas)
    eri_cas = eri_cas[ncore:nocc]
    for ix, (fcibox, norb, nelecas) in enumerate (zip (las.fciboxes,las.ncas_sub,las.nelecas_sub)):
        i = sum (las.ncas_sub[:ix])
        j = i + norb
        mo = mo_coeff[:,ncore+i:ncore+j]
        moH = mo.conj ().T
        h1e = moH @ h1e_ao @ mo
        h1e = [h1e, h1e]
        eri = eri_cas[i:j,i:j,i:j,i:j]
        for iy, solver in enumerate (fcibox.fcisolvers):
            nelec = fcibox._get_nelec (solver, nelecas)
            ndet = tuple ([cistring.num_strings (norb, n) for n in nelec])
            if isinstance (ci0[ix][iy], np.ndarray) and ci0[ix][iy].size==ndet[0]*ndet[1]: continue
            if hasattr (mo_coeff, 'orbsym'):
                solver.orbsym = mo_coeff.orbsym[ncore+i:ncore+j]
            hdiag_csf = solver.make_hdiag_csf (h1e, eri, norb, nelec)
            ci0[ix][iy] = solver.get_init_guess (norb, nelec, solver.nroots, hdiag_csf)[0]
    return ci0

def get_state_info (las):
    ''' Retrieve the quantum numbers defining the states of a LASSCF calculation '''
    nfrags, nroots = las.nfrags, las.nroots
    charges = np.zeros ((nroots, nfrags), dtype=np.int32)
    wfnsyms, spins, smults = charges.copy (), charges.copy (), charges.copy ()
    for ifrag, fcibox in enumerate (las.fciboxes):
     for iroot, solver in enumerate (fcibox.fcisolvers):
        nelec = fcibox._get_nelec (solver, las.nelecas_sub[ifrag])
        charges[iroot,ifrag] = np.sum (las.nelecas_sub[ifrag]) - np.sum (nelec)
        spins[iroot,ifrag] = nelec[0]-nelec[1]
        smults[iroot,ifrag] = solver.smult
        try:
            wfnsyms[iroot,ifrag] = solver.wfnsym or 0
        except ValueError as e:
            wfnsyms[iroot,ifrag] = symm.irrep_name2id (las.mol.groupname, solver.wfnsym)
    return charges, spins, smults, wfnsyms
   
def assert_no_duplicates (las, tab=None):
    log = lib.logger.new_logger (las, las.verbose)
    if tab is None: tab = np.stack (get_state_info (las), axis=-1)
    tab_uniq, uniq_idx, uniq_inv, uniq_cnts = np.unique (tab, return_index=True,
        return_inverse=True, return_counts=True, axis=0)
    idx_dupe = uniq_cnts>1
    try:
        err_str = ('LAS state basis has duplicates; details in logfile for '
                   'verbose >= INFO (4) [more details for verbose > INFO].\n'
                   '(Disable this assertion by passing assert_no_dupes=False '
                   'to the kernel, lasci, and state_average(_) functions.)')
        assert (~np.any (idx_dupe)), err_str
    except AssertionError as e:
        dupe_idx = uniq_idx[idx_dupe]
        dupe_cnts = uniq_cnts[idx_dupe]
        for i, (ix, cnt, col) in enumerate (zip (uniq_idx, uniq_cnts, tab_uniq)):
            if cnt==1: continue
            log.info ('State %d appears %d times', ix, cnt)
            idx_thisdupe = np.where (uniq_inv==i)[0]
            row = col.T
            log.debug ('As states {}'.format (idx_thisdupe))
            log.debug ('Charges = {}'.format (row[0]))
            log.debug ('2M_S = {}'.format (row[1]))
            log.debug ('2S+1 = {}'.format (row[2]))
            log.debug ('Wfnsyms = {}'.format (row[3]))
        raise e from None

def state_average_(las, weights=[0.5,0.5], charges=None, spins=None,
        smults=None, wfnsyms=None, assert_no_dupes=True):
    ''' Transform LASCI/LASSCF object into state-average LASCI/LASSCF 

    Args:
        las: LASCI/LASSCF instance

    Kwargs:
        weights: list of float; required
            E_SA = sum_i weights[i] E[i] is used to optimize the orbitals
        charges: 2d ndarray or nested list of integers
        spins: 2d ndarray or nested list of integers
            For the jth fragment in the ith state,
            neleca = (sum(las.nelecas_sub[j]) - charges[i][j] + spins[i][j]) // 2
            nelecb = (sum(las.nelecas_sub[j]) - charges[i][j] - spins[i][j]) // 2
            Defaults to
            charges[i][j] = 0
            spins[i][j] = las.nelecas_sub[j][0] - las.nelecas_sub[j][1]
        smults: 2d ndarray or nested list of integers
            For the jth fragment in the ith state,
            smults[i][j] = (2*s)+1
            where "s" is the total spin quantum number,
            S^2|j,i> = s*(s+1)|j,i>
            Defaults to
            smults[i][j] = abs (spins[i][j]) + 1
        wfnsyms: 2d ndarray or nested list of integers or strings
            For the jth fragment of the ith state,
            wfnsyms[i][j]
            identifies the point-group irreducible representation
            Defaults to all zeros (i.e., the totally-symmetric irrep)

    Returns:
        las: LASCI/LASSCF instance
            The first positional argument, modified in-place into a
            state-averaged LASCI/LASSCF instance.

    '''
    old_states = np.stack (get_state_info (las), axis=-1)
    nroots = len (weights)
    nfrags = las.nfrags
    if charges is None: charges = np.zeros ((nroots, nfrags), dtype=np.int32)
    if wfnsyms is None: wfnsyms = np.zeros ((nroots, nfrags), dtype=np.int32)
    if spins is None: spins = np.asarray ([[n[0]-n[1] for n in las.nelecas_sub] for i in weights]) 
    if smults is None: smults = np.abs (spins)+1 

    charges = np.asarray (charges)
    wfnsyms = np.asarray (wfnsyms)
    spins = np.asarray (spins)
    smults = np.asarray (smults)
    if np.issubsctype (wfnsyms.dtype, np.str_):
        wfnsyms_str = wfnsyms
        wfnsyms = np.zeros (wfnsyms_str.shape, dtype=np.int32)
        for ix, wfnsym in enumerate (wfnsyms_str.flat):
            try:
                wfnsyms.flat[ix] = symm.irrep_name2id (las.mol.groupname, wfnsym)
            except (TypeError, KeyError) as e:
                wfnsyms.flat[ix] = int (wfnsym)
    if nfrags == 1:
        charges = np.atleast_2d (np.squeeze (charges)).T
        wfnsyms = np.atleast_2d (np.squeeze (wfnsyms)).T
        spins = np.atleast_2d (np.squeeze (spins)).T
        smults = np.atleast_2d (np.squeeze (smults)).T
    new_states = np.stack ([charges, spins, smults, wfnsyms], axis=-1)
    if assert_no_dupes: assert_no_duplicates (las, tab=new_states)

    las.fciboxes = [get_h1e_zipped_fcisolver (state_average_n_mix (
        las, [csf_solver (las.mol, smult=s2p1).set (charge=c, spin=m2, wfnsym=ir)
              for c, m2, s2p1, ir in zip (c_r, m2_r, s2p1_r, ir_r)], weights).fcisolver)
        for c_r, m2_r, s2p1_r, ir_r in zip (charges.T, spins.T, smults.T, wfnsyms.T)]
    las.e_states = np.zeros (nroots)
    las.nroots = nroots
    las.weights = weights

    if las.ci is not None:
        log = lib.logger.new_logger(las, las.verbose)
        log.debug (("lasci.state_average: Cached CI vectors may be present.\n"
                    "Looking for matches between old and new LAS states..."))
        ci0 = [[None for i in range (nroots)] for j in range (nfrags)]
        new_states = np.stack ([charges, spins, smults, wfnsyms],
            axis=-1).reshape (nroots, nfrags*4)
        old_states = old_states.reshape (-1, nfrags*4)
        for iroot, row in enumerate (old_states):
            idx = np.all (new_states == row[None,:], axis=1)
            if np.count_nonzero (idx) == 1:
                jroot = np.where (idx)[0][0] 
                log.debug ("Old state {} -> New state {}".format (iroot, jroot))
                for ifrag in range (nfrags):
                    ci0[ifrag][jroot] = las.ci[ifrag][iroot]
            elif np.count_nonzero (idx) > 1:
                raise RuntimeError ("Duplicate states specified?\n{}".format (idx))
        las.ci = ci0
    return las

def state_average (las, weights=[0.5,0.5], charges=None, spins=None,
        smults=None, wfnsyms=None, assert_no_dupes=True):
    ''' A version of lasci.state_average_ that creates a copy instead of modifying the 
    LASCI/LASSCF method instance in place.

    See lasci.state_average_ docstring below:\n\n''' + state_average_.__doc__

    new_las = las.__class__(las._scf, las.ncas_sub, las.nelecas_sub)
    new_las.__dict__.update (las.__dict__)
    new_las.mo_coeff = las.mo_coeff.copy ()
    if getattr (las.mo_coeff, 'orbsym', None) is not None:
        new_las.mo_coeff = lib.tag_array (new_las.mo_coeff,
            orbsym=las.mo_coeff.orbsym)
    new_las.ci = None
    if las.ci is not None:
        new_las.ci = [[c2.copy () if isinstance (c2, np.ndarray) else None
            for c2 in c1] for c1 in las.ci]
    return state_average_(new_las, weights=weights, charges=charges, spins=spins,
        smults=smults, wfnsyms=wfnsyms, assert_no_dupes=assert_no_dupes)

def run_lasci (las, mo_coeff=None, ci0=None, verbose=0, assert_no_dupes=False):
    if assert_no_dupes: assert_no_duplicates (las)
    nao, nmo = mo_coeff.shape
    ncore, ncas = las.ncore, las.ncas
    nocc = ncore + ncas
    ncas_sub = las.ncas_sub
    nelecas_sub = las.nelecas_sub
    orbsym = getattr (mo_coeff, 'orbsym', None)
    if orbsym is not None: orbsym=orbsym[ncore:nocc]
    log = lib.logger.new_logger (las, verbose)

    h1eff, energy_core = casci.h1e_for_cas (las, mo_coeff=mo_coeff,
        ncas=las.ncas, ncore=las.ncore)
    h2eff = las.get_h2eff (mo_coeff) 
    if (ci0 is None or any ([c is None for c in ci0]) or
            any ([any ([c2 is None for c2 in c1]) for c1 in ci0])):
        ci0 = las.get_init_guess_ci (mo_coeff, h2eff, ci0)
    eri_cas = lib.numpy_helper.unpack_tril (
            h2eff.reshape (nmo*ncas, ncas*(ncas+1)//2)).reshape (nmo, ncas,
            ncas, ncas)[ncore:nocc]

    e_cas = np.empty (las.nroots)
    e_states = np.empty (las.nroots)
    ci1 = [[None for c2 in c1] for c1 in ci0]
    converged = True
    t = (lib.logger.process_clock(), lib.logger.perf_counter())
    for state in range (las.nroots):
        fcisolvers = [b.fcisolvers[state] for b in las.fciboxes]
        ci0_i = [c[state] for c in ci0]
        solver = ProductStateFCISolver (fcisolvers, stdout=las.stdout,
            verbose=verbose)
        # TODO: better handling of CSF symmetry quantum numbers in general
        for ix, s in enumerate (solver.fcisolvers):
            i = sum (ncas_sub[:ix])
            j = i + ncas_sub[ix]
            if orbsym is not None: s.orbsym = orbsym[i:j]
            s.norb = ncas_sub[ix]
            s.nelec = solver._get_nelec (s, nelecas_sub[ix])
            s.check_transformer_cache ()
        conv, e_i, ci_i = solver.kernel (h1eff, eri_cas, ncas_sub, nelecas_sub,
            ecore=0, ci0=ci0_i, orbsym=orbsym, conv_tol_grad=las.conv_tol_grad,
            conv_tol_self=las.conv_tol_self, max_cycle_macro=las.max_cycle_macro)
        e_cas[state] = e_i
        e_states[state] = e_i + energy_core
        for c1, c2, s, no, ne in zip (ci1, ci_i, solver.fcisolvers, ncas_sub, nelecas_sub):
            ne = solver._get_nelec (s, ne)
            ndet = tuple ([cistring.num_strings (no, n) for n in ne])
            c1[state] = c2.reshape (*ndet)
        if not conv: log.warn ('State %d LASCI not converged!', state)
        converged = converged and conv
        t = log.timer ('State {} LASCI'.format (state), *t)

    e_tot = np.dot (las.weights, e_states)
    return converged, e_tot, e_states, e_cas, ci1

class LASCINoSymm (casci.CASCI):

    def __init__(self, mf, ncas, nelecas, ncore=None, spin_sub=None, frozen=None, **kwargs):
        if isinstance(ncas,int):
            ncas = [ncas]
        ncas_tot = sum (ncas)
        nel_tot = [0, 0]
        new_nelecas = []
        for ix, nel in enumerate (nelecas):
            if isinstance (nel, (int, np.integer)):
                nb = nel // 2
                na = nb + (nel % 2)
            else:
                na, nb = nel
            new_nelecas.append ((na, nb))
            nel_tot[0] += na
            nel_tot[1] += nb
        nelecas = new_nelecas
        super().__init__(mf, ncas=ncas_tot, nelecas=nel_tot, ncore=ncore)
        if spin_sub is None: spin_sub = [1 for sub in ncas]
        self.ncas_sub = np.asarray (ncas)
        self.nelecas_sub = np.asarray (nelecas)
        self.frozen = frozen
        self.conv_tol_grad = 1e-4
        self.conv_tol_self = 1e-10
        self.ah_level_shift = 1e-8
        self.max_cycle_macro = 50
        self.max_cycle_micro = 5
        keys = set(('e_states', 'fciboxes', 'nroots', 'weights', 'ncas_sub', 'nelecas_sub',
                    'conv_tol_grad', 'conv_tol_self', 'max_cycle_macro', 'max_cycle_micro',
                    'ah_level_shift'))
        self._keys = set(self.__dict__.keys()).union(keys)
        self.fciboxes = []
        if isinstance(spin_sub,int):
            self.fciboxes.append(self._init_fcibox(spin_sub,self.nelecas_sub[0]))
        else:
            for smult, nel in zip (spin_sub, self.nelecas_sub):
                self.fciboxes.append (self._init_fcibox (smult, nel)) 
        self.nroots = 1
        self.weights = [1.0]
        self.e_states = [0.0]

    def _init_fcibox (self, smult, nel): 
        s = csf_solver (self.mol, smult=smult)
        s.spin = nel[0] - nel[1] 
        return get_h1e_zipped_fcisolver (state_average_n_mix (self, [s], [1.0]).fcisolver)

    @property
    def nfrags (self): return len (self.ncas_sub)

    def get_mo_slice (self, idx, mo_coeff=None):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        mo = mo_coeff[:,self.ncore:]
        for offs in self.ncas_sub[:idx]:
            mo = mo[:,offs:]
        mo = mo[:,:self.ncas_sub[idx]]
        return mo

    def ao2mo (self, mo_coeff=None):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        nao, nmo = mo_coeff.shape
        ncore, ncas = self.ncore, self.ncas
        nocc = ncore + ncas
        mo_cas = mo_coeff[:,ncore:nocc]
        mo = [mo_coeff, mo_cas, mo_cas, mo_cas]
        if getattr (self, 'with_df', None) is not None:
            # Store intermediate with one contracted ao index for faster calculation of exchange!
            bPmn = sparsedf_array (self.with_df._cderi)
            bmuP = bPmn.contract1 (mo_cas)
            buvP = np.tensordot (mo_cas.conjugate (), bmuP, axes=((0),(0)))
            eri_muxy = np.tensordot (bmuP, buvP, axes=((2),(2)))
            eri = np.tensordot (mo_coeff.conjugate (), eri_muxy, axes=((0),(0)))
            eri = lib.pack_tril (eri.reshape (nmo*ncas, ncas, ncas)).reshape (nmo, -1)
            eri = lib.tag_array (eri, bmPu=bmuP.transpose (0,2,1))
            if self.verbose > lib.logger.DEBUG:
                eri_comp = self.with_df.ao2mo (mo, compact=True)
                lib.logger.debug(self,"CDERI two-step error: {}".format(linalg.norm(eri-eri_comp)))
        elif getattr (self._scf, '_eri', None) is not None:
            eri = ao2mo.incore.general (self._scf._eri, mo, compact=True)
        else:
            eri = ao2mo.outcore.general_iofree (self.mol, mo, compact=True)
        if eri.shape != (nmo,ncas*ncas*(ncas+1)//2):
            try:
                eri = eri.reshape (nmo, ncas*ncas*(ncas+1)//2)
            except ValueError as e:
                assert (nmo == ncas), str (e)
                eri = ao2mo.restore ('2kl', eri, nmo).reshape (nmo, ncas*ncas*(ncas+1)//2)
        return eri

    def get_h2eff_slice (self, h2eff, idx, compact=None):
        ncas_cum = np.cumsum ([0] + self.ncas_sub.tolist ())
        i = ncas_cum[idx] 
        j = ncas_cum[idx+1]
        ncore = self.ncore
        nocc = ncore + self.ncas
        eri = h2eff[ncore:nocc,:].reshape (self.ncas*self.ncas, -1)
        ix_i, ix_j = np.tril_indices (self.ncas)
        eri = eri[(ix_i*self.ncas)+ix_j,:]
        eri = ao2mo.restore (1, eri, self.ncas)[i:j,i:j,i:j,i:j]
        if compact: eri = ao2mo.restore (compact, eri, j-i)
        return eri

    get_h1eff = get_h1cas = h1e_for_cas = h1e_for_cas
    get_h2eff = ao2mo
    '''
    def get_h2eff (self, mo_coeff=None):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if isinstance (self, _DFLASCI):
            mo_cas = mo_coeff[:,self.ncore:][:,:self.ncas]
            return self.with_df.ao2mo (mo_cas)
        return self.ao2mo (mo_coeff)
    '''

    get_fock = get_fock
    get_grad = get_grad
    _hop = lasci_sync.LASCI_HessianOperator
    def get_hop (self, mo_coeff=None, ci=None, ugg=None, **kwargs):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ci is None: ci = self.ci
        if ugg is None: ugg = self.get_ugg ()
        return self._hop (self, ugg, mo_coeff=mo_coeff, ci=ci, **kwargs)
    canonicalize = canonicalize

    def kernel(self, mo_coeff=None, ci0=None, casdm0_fr=None, conv_tol_grad=None,
            assert_no_dupes=False, verbose=None, _kern=None):
        if mo_coeff is None:
            mo_coeff = self.mo_coeff
        else:
            self.mo_coeff = mo_coeff
        if ci0 is None: ci0 = self.ci
        if verbose is None: verbose = self.verbose
        if conv_tol_grad is None: conv_tol_grad = self.conv_tol_grad
        if _kern is None: _kern = lasci_sync.kernel
        log = lib.logger.new_logger(self, verbose)

        if self.verbose >= lib.logger.WARN:
            self.check_sanity()
        self.dump_flags(log)

        # MRH: the below two lines are not the ideal solution to my problem...
        for fcibox in self.fciboxes:
            fcibox.verbose = self.verbose
            fcibox.stdout = self.stdout
        self.nroots = self.fciboxes[0].nroots
        self.weights = self.fciboxes[0].weights

        self.converged, self.e_tot, self.e_states, self.mo_energy, self.mo_coeff, self.e_cas, \
                self.ci, h2eff_sub, veff = _kern(self, mo_coeff, ci0=ci0, verbose=verbose, \
                casdm0_fr=casdm0_fr, conv_tol_grad=conv_tol_grad, assert_no_dupes=assert_no_dupes)

        return self.e_tot, self.e_cas, self.ci, self.mo_coeff, self.mo_energy, h2eff_sub, veff

    def states_make_casdm1s_sub (self, ci=None, ncas_sub=None, nelecas_sub=None, **kwargs):
        ''' Spin-separated 1-RDMs in the MO basis for each subspace in sequence '''
        if ci is None: ci = self.ci
        if ncas_sub is None: ncas_sub = self.ncas_sub
        if nelecas_sub is None: nelecas_sub = self.nelecas_sub
        if ci is None:
            return [np.zeros ((self.nroots,2,ncas,ncas)) for ncas in ncas_sub] 
        casdm1s = []
        for fcibox, ci_i, ncas, nelecas in zip (self.fciboxes, ci, ncas_sub, nelecas_sub):
            if ci_i is None:
                dm1a = dm1b = np.zeros ((ncas, ncas))
            else: 
                dm1a, dm1b = fcibox.states_make_rdm1s (ci_i, ncas, nelecas)
            casdm1s.append (np.stack ([dm1a, dm1b], axis=1))
        return casdm1s

    def make_casdm1s_sub (self, ci=None, ncas_sub=None, nelecas_sub=None,
            casdm1frs=None, w=None, **kwargs):
        if casdm1frs is None: casdm1frs = self.states_make_casdm1s_sub (ci=ci,
            ncas_sub=ncas_sub, nelecas_sub=nelecas_sub, **kwargs)
        if w is None: w = self.weights
        return [np.einsum ('rspq,r->spq', dm1, w) for dm1 in casdm1frs]

    def states_make_casdm1s (self, ci=None, ncas_sub=None, nelecas_sub=None,
            casdm1frs=None, **kwargs):
        if casdm1frs is None: casdm1frs = self.states_make_casdm1s_sub (ci=ci,
            ncas_sub=ncas_sub, nelecas_sub=nelecas_sub, **kwargs)
        return np.stack ([np.stack ([linalg.block_diag (*[dm1rs[iroot][ispin] 
                                                          for dm1rs in casdm1frs])
                                     for ispin in (0, 1)], axis=0)
                          for iroot in range (self.nroots)], axis=0)

    def states_make_casdm2_sub (self, ci=None, ncas_sub=None, nelecas_sub=None, **kwargs):
        ''' Spin-separated 1-RDMs in the MO basis for each subspace in sequence '''
        if ci is None: ci = self.ci
        if ncas_sub is None: ncas_sub = self.ncas_sub
        if nelecas_sub is None: nelecas_sub = self.nelecas_sub
        casdm2 = []
        for fcibox, ci_i, ncas, nel in zip (self.fciboxes, ci, ncas_sub, nelecas_sub):
            casdm2.append (fcibox.states_make_rdm12 (ci_i, ncas, nel)[-1])
        return casdm2

    def make_casdm2_sub (self, ci=None, ncas_sub=None, nelecas_sub=None, casdm2fr=None, **kwargs):
        if casdm2fr is None: casdm2fr = self.states_make_casdm2_sub (ci=ci, ncas_sub=ncas_sub,
            nelecas_sub=nelecas_sub, **kwargs)
        return [np.einsum ('rijkl,r->ijkl', dm2, box.weights)
                for dm2, box in zip (casdm2fr, self.fciboxes)]

    def states_make_rdm1s (self, mo_coeff=None, ci=None, ncas_sub=None,
            nelecas_sub=None, casdm1rs=None, casdm1frs=None, **kwargs):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ci is None: ci = self.ci
        if ncas_sub is None: ncas_sub = self.ncas_sub
        if nelecas_sub is None: nelecas_sub = self.nelecas_sub
        if casdm1rs is None: casdm1rs = self.states_make_casdm1s (ci=ci, 
            ncas_sub=ncas_sub, nelecas_sub=nelecas_sub, casdm1frs=casdm1frs, 
            **kwargs)
        mo_core = mo_coeff[:,:self.ncore]
        mo_cas = mo_coeff[:,self.ncore:][:,:self.ncas]
        dm1rs = np.tensordot (mo_cas.conj (), np.dot (casdm1rs, mo_cas.conj ().T), axes=((1),(2)))
        dm1rs = dm1rs.transpose (1,2,0,3)
        dm1rs += (mo_core @ mo_core.conj ().T)[None,None,:,:]
        return dm1rs

    def make_rdm1s_sub (self, mo_coeff=None, ci=None, ncas_sub=None,
            nelecas_sub=None, include_core=False, casdm1s_sub=None, **kwargs):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ci is None: ci = self.ci
        if ncas_sub is None: ncas_sub = self.ncas_sub
        if nelecas_sub is None: nelecas_sub = self.nelecas_sub
        if casdm1s_sub is None: casdm1s_sub = self.make_casdm1s_sub (ci=ci,
            ncas_sub=ncas_sub, nelecas_sub=nelecas_sub, **kwargs)
        ''' Same as make_casdm1s_sub, but in the ao basis '''
        rdm1s = []
        for idx, casdm1s in enumerate (casdm1s_sub):
            mo = self.get_mo_slice (idx, mo_coeff=mo_coeff)
            moH = mo.conjugate ().T
            rdm1s.append (np.tensordot (mo, np.dot (casdm1s,moH), axes=((1),(1))).transpose(1,0,2))
        if include_core and self.ncore:
            mo_core = mo_coeff[:,:self.ncore]
            moH_core = mo_core.conjugate ().T
            dm_core = mo_core @ moH_core
            rdm1s = [np.stack ([dm_core, dm_core], axis=0)] + rdm1s
        rdm1s = np.stack (rdm1s, axis=0)
        return rdm1s

    def make_rdm1_sub (self, **kwargs):
        return self.make_rdm1s_sub (**kwargs).sum (1)

    def make_rdm1s (self, mo_coeff=None, ncore=None, **kwargs):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ncore is None: ncore = self.ncore
        mo = mo_coeff[:,:ncore]
        moH = mo.conjugate ().T
        dm_core = mo @ moH
        dm_cas = self.make_rdm1s_sub (mo_coeff=mo_coeff, **kwargs).sum (0)
        return dm_core[None,:,:] + dm_cas

    def make_rdm1 (self, **kwargs):
        return self.make_rdm1s (**kwargs).sum (0)

    def make_casdm1s (self, **kwargs):
        ''' Make the full-dimensional casdm1s spanning the collective active space '''
        casdm1s_sub = self.make_casdm1s_sub (**kwargs)
        casdm1a = linalg.block_diag (*[dm[0] for dm in casdm1s_sub])
        casdm1b = linalg.block_diag (*[dm[1] for dm in casdm1s_sub])
        return np.stack ([casdm1a, casdm1b], axis=0)

    def make_casdm1 (self, **kwargs):
        ''' Spin-sum make_casdm1s '''
        return self.make_casdm1s (**kwargs).sum (0)

    def states_make_casdm2 (self, ci=None, ncas_sub=None, nelecas_sub=None, 
            casdm1frs=None, casdm2fr=None, **kwargs):
        ''' Make the full-dimensional casdm2 spanning the collective active space '''
        log = lib.logger.new_logger (self, verbose)
        log.warn (("You have found yourself in states_make_casdm2, which is "
                   "a very bad piece of code that Matt should be avoiding. "
                   "Please yell at him about this at earliest convenience."))
        if ci is None: ci = self.ci
        if ncas_sub is None: ncas_sub = self.ncas_sub
        if nelecas_sub is None: nelecas_sub = self.nelecas_sub
        if casdm1frs is None: casdm1frs = self.states_make_casdm1s_sub (ci=ci)
        if casdm2fr is None: casdm2fr = self.states_make_casdm2_sub (ci=ci,
            ncas_sub=ncas_sub, nelecas_sub=nelecas_sub, **kwargs)
        ncas = sum (ncas_sub)
        ncas_cum = np.cumsum ([0] + ncas_sub.tolist ())
        casdm2r = np.zeros ((self.nroots,ncas,ncas,ncas,ncas))
        # Diagonal 
        for isub, dm2 in enumerate (casdm2fr):
            i = ncas_cum[isub]
            j = ncas_cum[isub+1]
            casdm2r[:, i:j, i:j, i:j, i:j] = dm2
        # Off-diagonal
        for (isub1, dm1s1_r), (isub2, dm1s2_r) in combinations (enumerate (casdm1frs), 2):
            i = ncas_cum[isub1]
            j = ncas_cum[isub1+1]
            k = ncas_cum[isub2]
            l = ncas_cum[isub2+1]
            for dm1s1, dm1s2, casdm2 in zip (dm1s1_r, dm1s2_r, casdm2r):
                dma1, dmb1 = dm1s1[0], dm1s1[1]
                dma2, dmb2 = dm1s2[0], dm1s2[1]
                # Coulomb slice
                casdm2[i:j, i:j, k:l, k:l] = np.multiply.outer (dma1+dmb1, dma2+dmb2)
                casdm2[k:l, k:l, i:j, i:j] = casdm2[i:j, i:j, k:l, k:l].transpose (2,3,0,1)
                # Exchange slice
                casdm2[i:j, k:l, k:l, i:j] = -(np.multiply.outer (dma1, dma2)
                                               +np.multiply.outer (dmb1, dmb2)).transpose (0,3,2,1)
                casdm2[k:l, i:j, i:j, k:l] = casdm2[i:j, k:l, k:l, i:j].transpose (1,0,3,2)
        return casdm2r 

    def make_casdm2 (self, ci=None, ncas_sub=None, nelecas_sub=None, 
            casdm2r=None, casdm2f=None, casdm1frs=None, casdm2fr=None,
            **kwargs):
        ''' Make the full-dimensional casdm2 spanning the collective active space '''
        if casdm2r is not None: 
            return np.einsum ('rijkl,r->ijkl', casdm2r, self.weights)
        if ci is None: ci = self.ci
        if ncas_sub is None: ncas_sub = self.ncas_sub
        if nelecas_sub is None: nelecas_sub = self.nelecas_sub
        if casdm1frs is None: casdm1frs = self.states_make_casdm1s_sub (ci=ci,
            ncas_sub=ncas_sub, nelecas_sub=nelecas_sub)
        if casdm2f is None: casdm2f = self.make_casdm2_sub (ci=ci,
            ncas_sub=ncas_sub, nelecas_sub=nelecas_sub, casdm2fr=casdm2fr)
        ncas = sum (ncas_sub)
        ncas_cum = np.cumsum ([0] + ncas_sub.tolist ())
        weights = self.weights
        casdm2 = np.zeros ((ncas,ncas,ncas,ncas))
        # Diagonal 
        for isub, dm2 in enumerate (casdm2f):
            i = ncas_cum[isub]
            j = ncas_cum[isub+1]
            casdm2[i:j, i:j, i:j, i:j] = dm2
        # Off-diagonal
        for (isub1, dm1rs1), (isub2, dm1rs2) in combinations (enumerate (casdm1frs), 2):
            i = ncas_cum[isub1]
            j = ncas_cum[isub1+1]
            k = ncas_cum[isub2]
            l = ncas_cum[isub2+1]
            dma1r, dmb1r = dm1rs1[:,0], dm1rs1[:,1]
            dma2r, dmb2r = dm1rs2[:,0], dm1rs2[:,1]
            dm1r = dma1r + dmb1r
            dm2r = dma2r + dmb2r
            # Coulomb slice
            casdm2[i:j, i:j, k:l, k:l] = lib.einsum ('r,rij,rkl->ijkl', weights, dm1r, dm2r)
            casdm2[k:l, k:l, i:j, i:j] = casdm2[i:j, i:j, k:l, k:l].transpose (2,3,0,1)
            # Exchange slice
            d2exc = (lib.einsum ('rij,rkl->rilkj', dma1r, dma2r)
                   + lib.einsum ('rij,rkl->rilkj', dmb1r, dmb2r))
            casdm2[i:j, k:l, k:l, i:j] -= np.tensordot (weights, d2exc, axes=1)
            casdm2[k:l, i:j, i:j, k:l] = casdm2[i:j, k:l, k:l, i:j].transpose (1,0,3,2)
        return casdm2 

    def get_veff (self, mol=None, dm1s=None, hermi=1, spin_sep=False, **kwargs):
        ''' Returns a spin-summed veff! If dm1s isn't provided, builds from self.mo_coeff, self.ci
            etc. '''
        if mol is None: mol = self.mol
        nao = mol.nao_nr ()
        if dm1s is None: dm1s = self.make_rdm1 (include_core=True, **kwargs).reshape (nao, nao)
        dm1s = np.asarray (dm1s)
        if dm1s.ndim == 2: dm1s = dm1s[None,:,:]
        if isinstance (self, _DFLASCI):
            vj, vk = self.with_df.get_jk(dm1s, hermi=hermi)
        else:
            vj, vk = self._scf.get_jk(mol, dm1s, hermi=hermi)
        if spin_sep:
            assert (dm1s.shape[0] == 2)
            return vj.sum (0)[None,:,:] - vk
        else:
            veff = np.stack ([j - k/2 for j, k in zip (vj, vk)], axis=0)
            return np.squeeze (veff)

    def split_veff (self, veff, h2eff_sub, mo_coeff=None, ci=None, casdm1s_sub=None):
        ''' Split a spin-summed veff into alpha and beta terms using the h2eff eri array.
        Note that this will omit v(up_active - down_active)^virtual_inactive by necessity; 
        this won't affect anything because the inactive density matrix has no spin component.
        On the other hand, it ~is~ necessary to correctly do 

        v(up_active - down_active)^unactive_active

        in order to calculate the external orbital gradient at the end of the calculation.
        This means that I need h2eff_sub spanning both at least two active subspaces
        ~and~ the full orbital range. '''
        veff_c = veff.copy ()
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ci is None: ci = self.ci
        if casdm1s_sub is None: casdm1s_sub = self.make_casdm1s_sub (ci = ci)
        ncore = self.ncore
        ncas = self.ncas
        nocc = ncore + ncas
        nao, nmo = mo_coeff.shape
        moH_coeff = mo_coeff.conjugate ().T
        smo_coeff = self._scf.get_ovlp () @ mo_coeff
        smoH_coeff = smo_coeff.conjugate ().T
        veff_s = np.zeros_like (veff_c)
        for ix, (ncas_i, casdm1s) in enumerate (zip (self.ncas_sub, casdm1s_sub)):
            i = sum (self.ncas_sub[:ix])
            j = i + ncas_i
            eri_k = h2eff_sub.reshape (nmo, ncas, -1)[:,i:j,...].reshape (nmo*ncas_i, -1)
            eri_k = lib.numpy_helper.unpack_tril (eri_k)[:,i:j,:]
            eri_k = eri_k.reshape (nmo, ncas_i, ncas_i, ncas)
            sdm = casdm1s[0] - casdm1s[1]
            vk_pa = -np.tensordot (eri_k, sdm, axes=((1,2),(0,1))) / 2
            veff_s[:,ncore:nocc] += vk_pa
            veff_s[ncore:nocc,:] += vk_pa.T
            veff_s[ncore:nocc,ncore:nocc] -= vk_pa[ncore:nocc,:] / 2
            veff_s[ncore:nocc,ncore:nocc] -= vk_pa[ncore:nocc,:].T / 2
        veff_s = smo_coeff @ veff_s @ smoH_coeff
        veffa = veff_c + veff_s
        veffb = veff_c - veff_s
        return np.stack ([veffa, veffb], axis=0)
         

    def states_energy_elec (self, mo_coeff=None, ncore=None, ncas=None,
            ncas_sub=None, nelecas_sub=None, ci=None, h2eff=None, veff=None, 
            casdm1frs=None, casdm2fr=None, **kwargs):
        ''' Since the LASCI energy cannot be calculated as simply as ecas + ecore, I need this fn
            Here, veff has to be the TRUE AND ACCURATE, ACTUAL veff_rs!'''
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ncore is None: ncore = self.ncore
        if ncas is None: ncas = self.ncas
        if ncas_sub is None: ncas_sub = self.ncas_sub
        if nelecas_sub is None: nelecas_sub = self.nelecas_sub
        if ci is None: ci = self.ci
        if h2eff is None: h2eff = self.get_h2eff (mo_coeff)
        if casdm1frs is None: casdm1frs = self.states_make_casdm1s_sub (ci=ci, ncas_sub=ncas_sub,
                                                                        nelecas_sub=nelecas_sub)
        if casdm2fr is None: casdm2fr = self.states_make_casdm2_sub (ci=ci, ncas_sub=ncas_sub,
                                                                     nelecas_sub=nelecas_sub)

        dm1rs = self.states_make_rdm1s (mo_coeff=mo_coeff, ci=ci,
            ncas_sub=ncas_sub, nelecas_sub=nelecas_sub, casdm1frs=casdm1frs)
        if veff is None: veff = np.stack ([self.get_veff (dm1s = dm1s, spin_sep=True)
                                           for dm1s in dm1rs], axis=0)
        assert (veff.ndim == 4)

        energy_elec = []
        for idx, (dm1s, v) in enumerate (zip (dm1rs, veff)):
            casdm1fs = [dm[idx] for dm in casdm1frs]
            casdm2f = [dm[idx] for dm in casdm2fr]
            
            # 1-body veff terms
            h1e = self.get_hcore ()[None,:,:] + v/2
            e1 = np.dot (h1e.ravel (), dm1s.ravel ())

            # 2-body cumulant terms
            e2 = 0
            for isub, (dm1s, dm2) in enumerate (zip (casdm1fs, casdm2f)):
                dm1a, dm1b = dm1s[0], dm1s[1]
                dm1 = dm1a + dm1b
                cdm2 = dm2 - np.multiply.outer (dm1, dm1)
                cdm2 += np.multiply.outer (dm1a, dm1a).transpose (0,3,2,1)
                cdm2 += np.multiply.outer (dm1b, dm1b).transpose (0,3,2,1)
                eri = self.get_h2eff_slice (h2eff, isub)
                te2 = np.tensordot (eri, cdm2, axes=4) / 2
                e2 += te2
            energy_elec.append (e1 + e2)
            self._e1_ref = e1
            self._e2_ref = e2

        return energy_elec

    def energy_elec (self, mo_coeff=None, ncore=None, ncas=None,
            ncas_sub=None, nelecas_sub=None, ci=None, h2eff=None, veff=None,
            casdm1frs=None, casdm2fr=None, **kwargs):
        ''' Since the LASCI energy cannot be calculated as simply as ecas + ecore, I need this '''
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ncore is None: ncore = self.ncore
        if ncas is None: ncas = self.ncas
        if ncas_sub is None: ncas_sub = self.ncas_sub
        if nelecas_sub is None: nelecas_sub = self.nelecas_sub
        if ci is None: ci = self.ci
        if h2eff is None: h2eff = self.get_h2eff (mo_coeff)
        casdm1s_sub = self.make_casdm1s_sub (ci=ci, ncas_sub=ncas_sub, nelecas_sub=nelecas_sub,
                                             casdm1frs=casdm1frs)
        if veff is None:
            veff = self.get_veff (dm1s = self.make_rdm1(mo_coeff=mo_coeff,casdm1s_sub=casdm1s_sub))
            veff = self.split_veff (veff, h2eff, mo_coeff=mo_coeff, casdm1s_sub=casdm1s_sub)

        # 1-body veff terms
        h1e = self.get_hcore ()[None,:,:] + veff/2
        dm1s = self.make_rdm1s (mo_coeff=mo_coeff, ncore=ncore, ncas_sub=ncas_sub,
            nelecas_sub=nelecas_sub, casdm1s_sub=casdm1s_sub)
        e1 = np.dot (h1e.ravel (), dm1s.ravel ())

        # 2-body cumulant terms
        casdm1s = self.make_casdm1s (ci=ci, ncas_sub=ncas_sub, 
            nelecas_sub=nelecas_sub, casdm1frs=casdm1frs)
        casdm1 = casdm1s.sum (0)
        casdm2 = self.make_casdm2 (ci=ci, ncas_sub=ncas_sub,
            nelecas_sub=nelecas_sub, casdm1frs=casdm1frs, casdm2fr=casdm2fr)
        casdm2 -= np.multiply.outer (casdm1, casdm1)
        casdm2 += np.multiply.outer (casdm1s[0], casdm1s[0]).transpose (0,3,2,1)
        casdm2 += np.multiply.outer (casdm1s[1], casdm1s[1]).transpose (0,3,2,1)
        ncore, ncas, nocc = self.ncore, self.ncas, self.ncore + self.ncas
        eri = lib.numpy_helper.unpack_tril (h2eff[ncore:nocc].reshape (ncas*ncas, -1))
        eri = eri.reshape ([ncas,]*4)
        e2 = np.tensordot (eri, casdm2, axes=4)/2

        e0 = self.energy_nuc ()
        self._e1_test = e1
        self._e2_test = e2
        return e1 + e2

    _ugg = lasci_sync.LASCI_UnitaryGroupGenerators
    def get_ugg (self, mo_coeff=None, ci=None):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ci is None: ci = self.ci
        return self._ugg (self, mo_coeff, ci)

    def cderi_ao2mo (self, mo_i, mo_j, compact=False):
        assert (isinstance (self, _DFLASCI))
        nmo_i, nmo_j = mo_i.shape[-1], mo_j.shape[-1]
        if compact:
            assert (nmo_i == nmo_j)
            bPij = np.empty ((self.with_df.get_naoaux (), nmo_i*(nmo_i+1)//2), dtype=mo_i.dtype)
        else:
            bPij = np.empty ((self.with_df.get_naoaux (), nmo_i, nmo_j), dtype=mo_i.dtype)
        ijmosym, mij_pair, moij, ijslice = ao2mo.incore._conc_mos (mo_i, mo_j, compact=compact)
        b0 = 0
        for eri1 in self.with_df.loop ():
            b1 = b0 + eri1.shape[0]
            eri2 = bPij[b0:b1]
            eri2 = ao2mo._ao2mo.nr_e2 (eri1, moij, ijslice, aosym='s2', mosym=ijmosym, out=eri2)
            b0 = b1
        return bPij

    def fast_veffa (self, casdm1s_sub, h2eff_sub, mo_coeff=None, ci=None, _full=False):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ci is None: ci = self.ci
        assert (isinstance (self, _DFLASCI) or _full)
        ncore = self.ncore
        ncas_sub = self.ncas_sub
        ncas = sum (ncas_sub)
        nocc = ncore + ncas
        nao, nmo = mo_coeff.shape

        mo_cas = mo_coeff[:,ncore:nocc]
        moH_cas = mo_cas.conjugate ().T
        moH_coeff = mo_coeff.conjugate ().T
        dma = linalg.block_diag (*[dm[0] for dm in casdm1s_sub])
        dmb = linalg.block_diag (*[dm[1] for dm in casdm1s_sub])
        casdm1s = np.stack ([dma, dmb], axis=0)
        if not (isinstance (self, _DFLASCI)):
            dm1s = np.dot (mo_cas, np.dot (casdm1s, moH_cas)).transpose (1,0,2)
            return self.get_veff (dm1s = dm1s, spin_sep=True)
        casdm1 = casdm1s.sum (0)
        dm1 = np.dot (mo_cas, np.dot (casdm1, moH_cas))
        bPmn = sparsedf_array (self.with_df._cderi)

        # vj
        dm_tril = dm1 + dm1.T - np.diag (np.diag (dm1.T))
        rho = np.dot (bPmn, lib.pack_tril (dm_tril))
        vj = lib.unpack_tril (np.dot (rho, bPmn))

        # vk
        bmPu = h2eff_sub.bmPu
        if _full:
            vmPsu = np.dot (bmPu, casdm1s)
            vk = np.tensordot (vmPsu, bmPu, axes=((1,3),(1,2))).transpose (1,0,2)
            return vj[None,:,:] - vk
        else:
            vmPu = np.dot (bmPu, casdm1)
            vk = np.tensordot (vmPu, bmPu, axes=((1,2),(1,2)))
            return vj - vk/2

    def lasci (self, mo_coeff=None, ci0=None, verbose=None,
            assert_no_dupes=False):
        if mo_coeff is None: mo_coeff=self.mo_coeff
        if ci0 is None: ci0 = self.ci
        if verbose is None: verbose = self.verbose
        converged, e_tot, e_states, e_cas, ci = run_lasci (
            self, mo_coeff=mo_coeff, ci0=ci0, verbose=verbose,
            assert_no_dupes=assert_no_dupes)
        self.converged, self.ci = converged, ci
        self.e_tot, self.e_states, self.e_cas = e_tot, e_states, e_cas
        return self.converged, self.e_tot, self.e_states, self.e_cas, self.ci

    state_average = state_average
    state_average_ = state_average_
    lassi = lassi
    las2cas_civec = las2cas_civec
    assert_no_duplicates = assert_no_duplicates
    get_init_guess_ci = get_init_guess_ci

class LASCISymm (casci_symm.CASCI, LASCINoSymm):

    def __init__(self, mf, ncas, nelecas, ncore=None, spin_sub=None, wfnsym_sub=None, frozen=None,
                 **kwargs):
        LASCINoSymm.__init__(self, mf, ncas, nelecas, ncore=ncore, spin_sub=spin_sub,
                             frozen=frozen, **kwargs)
        if wfnsym_sub is None: wfnsym_sub = [0 for icas in self.ncas_sub]
        for wfnsym, frag in zip (wfnsym_sub, self.fciboxes):
            if isinstance (wfnsym, (str, np.str_)):
                wfnsym = symm.irrep_name2id (self.mol.groupname, wfnsym)
            frag.fcisolvers[0].wfnsym = wfnsym

    make_rdm1s = LASCINoSymm.make_rdm1s
    make_rdm1 = LASCINoSymm.make_rdm1
    get_veff = LASCINoSymm.get_veff
    get_h1eff = get_h1cas = h1e_for_cas 
    _ugg = lasci_sync.LASCISymm_UnitaryGroupGenerators

    @property
    def wfnsym (self):
        ''' This now returns the product of the irreps of the subspaces '''
        wfnsym = [0,]*self.nroots
        for frag in self.fciboxes:
            for state, solver in enumerate (frag.fcisolvers):
                wfnsym[state] ^= solver.wfnsym
        if self.nroots == 1: wfnsym = wfnsym[0]
        return wfnsym
    @wfnsym.setter
    def wfnsym (self, ir):
        raise RuntimeError (("Cannot assign the whole-system symmetry of a LASCI wave function. "
                             "Address fciboxes[ifrag].fcisolvers[istate].wfnsym instead."))

    def kernel(self, mo_coeff=None, ci0=None, casdm0_fr=None, verbose=None, assert_no_dupes=False):
        if mo_coeff is None:
            mo_coeff = self.mo_coeff
        if ci0 is None:
            ci0 = self.ci

        # Initialize/overwrite mo_coeff.orbsym. Don't pass ci0 because it's not the right shape
        lib.logger.info (self, ("LASCI lazy hack note: lines below reflect the point-group "
                                "symmetry of the whole molecule but not of the individual "
                                "subspaces"))
        mo_coeff = self.mo_coeff = self.label_symmetry_(mo_coeff)
        return LASCINoSymm.kernel(self, mo_coeff=mo_coeff, ci0=ci0,
            casdm0_fr=casdm0_fr, verbose=verbose, assert_no_dupes=assert_no_dupes)

    def canonicalize (self, mo_coeff=None, ci=None, natorb_casdm1=None, veff=None, h2eff_sub=None):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        mo_coeff = self.label_symmetry_(mo_coeff)
        return canonicalize (self, mo_coeff=mo_coeff, ci=ci, natorb_casdm1=natorb_casdm1,
                             h2eff_sub=h2eff_sub, orbsym=mo_coeff.orbsym)

    def label_symmetry_(self, mo_coeff=None):
        if mo_coeff is None: mo_coeff=self.mo_coeff
        ncore = self.ncore
        ncas_sub = self.ncas_sub
        nocc = ncore + sum (ncas_sub)
        mo_coeff[:,:ncore] = symm.symmetrize_space (self.mol, mo_coeff[:,:ncore])
        for isub, ncas in enumerate (ncas_sub):
            i = ncore + sum (ncas_sub[:isub])
            j = i + ncas
            mo_coeff[:,i:j] = symm.symmetrize_space (self.mol, mo_coeff[:,i:j])
        mo_coeff[:,nocc:] = symm.symmetrize_space (self.mol, mo_coeff[:,nocc:])
        orbsym = symm.label_orb_symm (self.mol, self.mol.irrep_id,
                                      self.mol.symm_orb, mo_coeff,
                                      s=self._scf.get_ovlp ())
        mo_coeff = lib.tag_array (mo_coeff, orbsym=orbsym)
        return mo_coeff
        

        
