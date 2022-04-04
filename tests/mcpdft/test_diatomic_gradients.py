import numpy as np
from scipy import linalg
from pyscf import gto, scf, df, mcscf, lib
from pyscf.data.nist import BOHR
from mrh.my_pyscf import mcpdft
from mrh.my_pyscf.fci import csf_solver
from mrh.my_pyscf.grad.cmspdft import diab_response, diab_grad, diab_response_o0, diab_grad_o0
from mrh.my_pyscf.grad import sipdft as sipdft_grad
from mrh.my_pyscf.df.grad import dfsacasscf, dfsipdft
import unittest, math

def diatomic (atom1, atom2, r, fnal, basis, ncas, nelecas, nstates,
              charge=None, spin=None, symmetry=False, cas_irrep=None,
              density_fit=False):
    xyz = '{:s} 0.0 0.0 0.0; {:s} {:.3f} 0.0 0.0'.format (atom1, atom2, r)
    mol = gto.M (atom=xyz, basis=basis, charge=charge, spin=spin, symmetry=symmetry, verbose=0, output='/dev/null')
    mf = scf.RHF (mol)
    if density_fit: mf = mf.density_fit (auxbasis = df.aug_etb (mol))
    mc = mcpdft.CASSCF (mf.run (), fnal, ncas, nelecas, grids_level=9)
    if spin is not None: smult = spin+1
    else: smult = (mol.nelectron % 2) + 1
    mc.fcisolver = csf_solver (mol, smult=smult)
    mc = mc.state_interaction ([1.0/float(nstates),]*nstates, 'cms')
    mc.conv_tol = mc.conv_tol_diabatize = 1e-12
    mo = None
    if symmetry and (cas_irrep is not None):
        mo = mc.sort_mo_by_irrep (cas_irrep)
    mc.kernel (mo)
    if density_fit: return dfsipdft.Gradients (mc)
    return mc.nuc_grad_method ()

def tearDownModule():
    global diatomic
    del diatomic

class KnownValues(unittest.TestCase):

    def test_grad_h2_cms3ftlda22_sto3g (self):
        # z_orb:    no
        # z_ci:     no
        # z_is:     no
        mc_grad = diatomic ('H', 'H', 1.3, 'ftLDA,VWN3', 'STO-3G', 2, 2, 3)
        de_ref = [0.226842531, -0.100538192, -0.594129499] 
        # Numerical from this software
        for i in range (3):
         with self.subTest (state=i):
            de = mc_grad.kernel (state=i) [1,0] / BOHR
            self.assertAlmostEqual (de, de_ref[i], 5)

    def test_grad_h2_cms2ftlda22_sto3g (self):
        # z_orb:    no
        # z_ci:     yes
        # z_is:     no
        mc_grad = diatomic ('H', 'H', 1.3, 'ftLDA,VWN3', 'STO-3G', 2, 2, 2)
        de_ref = [0.125068648, -0.181916973] 
        # Numerical from this software
        for i in range (2):
         with self.subTest (state=i):
            de = mc_grad.kernel (state=i) [1,0] / BOHR
            self.assertAlmostEqual (de, de_ref[i], 6)

    def test_grad_h2_cms3ftlda22_631g (self):
        # z_orb:    yes
        # z_ci:     no
        # z_is:     no
        mc_grad = diatomic ('H', 'H', 1.3, 'ftLDA,VWN3', '6-31G', 2, 2, 3)
        de_ref = [0.1717391582, -0.05578044075, -0.418332932] 
        # Numerical from this software
        for i in range (3):
         with self.subTest (state=i):
            de = mc_grad.kernel (state=i) [1,0] / BOHR
            self.assertAlmostEqual (de, de_ref[i], 5)

    def test_grad_h2_cms2ftlda22_631g (self):
        # z_orb:    yes
        # z_ci:     yes
        # z_is:     no
        mc_grad = diatomic ('H', 'H', 1.3, 'ftLDA,VWN3', '6-31G', 2, 2, 2)
        de_ref = [0.1046653372, -0.07056592067] 
        # Numerical from this software
        for i in range (2):
         with self.subTest (state=i):
            de = mc_grad.kernel (state=i) [1,0] / BOHR
            self.assertAlmostEqual (de, de_ref[i], 6)

    def test_grad_lih_cms2ftlda44_sto3g (self):
        # z_orb:    no
        # z_ci:     yes
        # z_is:     yes
        mc_grad = diatomic ('Li', 'H', 1.8, 'ftLDA,VWN3', 'STO-3G', 4, 4, 2, symmetry=True, cas_irrep={'A1': 4})
        de_ref = [0.0659740768, -0.005995224082] 
        # Numerical from this software
        for i in range (2):
         with self.subTest (state=i):
            de = mc_grad.kernel (state=i) [1,0] / BOHR
            self.assertAlmostEqual (de, de_ref[i], 6)

    def test_grad_lih_cms3ftlda22_sto3g (self):
        # z_orb:    yes
        # z_ci:     no
        # z_is:     yes
        mc_grad = diatomic ('Li', 'H', 2.5, 'ftLDA,VWN3', 'STO-3G', 2, 2, 3)
        de_ref = [0.09307779491, 0.07169985876, -0.08034177097] 
        # Numerical from this software
        for i in range (3):
         with self.subTest (state=i):
            de = mc_grad.kernel (state=i) [1,0] / BOHR
            self.assertAlmostEqual (de, de_ref[i], 6)

    def test_grad_lih_cms2ftlda22_sto3g (self):
        # z_orb:    yes
        # z_ci:     yes
        # z_is:     yes
        mc_grad = diatomic ('Li', 'H', 2.5, 'ftLDA,VWN3', 'STO-3G', 2, 2, 2)
        de_ref = [0.1071803011, 0.03972321867] 
        # Numerical from this software
        for i in range (2):
         with self.subTest (state=i):
            de = mc_grad.kernel (state=i) [1,0] / BOHR
            self.assertAlmostEqual (de, de_ref[i], 6)

    def test_grad_lih_cms2ftlda22_sto3g_df (self):
        # z_orb:    yes
        # z_ci:     yes
        # z_is:     yes
        mc_grad = diatomic ('Li', 'H', 2.5, 'ftLDA,VWN3', 'STO-3G', 2, 2, 2, density_fit=True)
        de_ref = [0.1074553399, 0.03956955205] 
        # Numerical from this software
        for i in range (2):
         with self.subTest (state=i):
            de = mc_grad.kernel (state=i) [1,0] / BOHR
            self.assertAlmostEqual (de, de_ref[i], 6)

if __name__ == "__main__":
    print("Full Tests for CMS-PDFT gradients of diatomic molecules")
    unittest.main()






