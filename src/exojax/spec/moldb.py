"""Molecular database (MDB) class.

* MdbExomol is the MDB for ExoMol
* MdbHit is the MDB for HITRAN or HITEMP
"""
import os
import numpy as np
import jax.numpy as jnp
from jax import jit, vmap
import pathlib
import vaex
from exojax.spec import hapi, exomolapi, exomol, atomllapi, atomll, hitranapi
from exojax.spec.hitran import gamma_natural as gn
from exojax.utils.constants import hcperk, Tref

__all__ = ['MdbExomol', 'MdbHit', 'AdbVald', 'AdbKurucz']

explanation_states = "Note: Couldn't find the hdf5 format. We convert data to the hdf5 format. After the second time, it will become much faster."
explanation_trans = "Note: Couldn't find the hdf5 format. We convert data to the hdf5 format. After the second time, it will become much faster."
warning_old_exojax = 'It seems that the hdf5 file for the transition file was created using the old version of exojax<1.1. Try again after removing '


class MdbExomol(object):
    """molecular database of ExoMol.

    MdbExomol is a class for ExoMol.

    Attributes:
        nurange: nu range [min,max] (cm-1)
        nu_lines (nd array): line center (cm-1)
        Sij0 (nd array): line strength at T=Tref (cm)
        dev_nu_lines (jnp array): line center in device (cm-1)
        logsij0 (jnp array): log line strength at T=Tref
        A (jnp array): Einstein A coeeficient
        gamma_natural (jnp array): gamma factor of the natural broadening
        elower (jnp array): the lower state energy (cm-1)
        gpp (jnp array): statistical weight
        jlower (jnp array): J_lower
        jupper (jnp array): J_upper
        n_Texp (jnp array): temperature exponent
        alpha_ref (jnp array): alpha_ref (gamma0), Lorentzian half-width at reference temperature and pressure in cm-1/bar
        n_Texp_def: default temperature exponent in .def file, used for jlower not given in .broad
        alpha_ref_def: default alpha_ref (gamma0) in .def file, used for jlower not given in .broad
    """

    def __init__(self, path, nurange=[-np.inf, np.inf], margin=0.0, crit=0., Ttyp=1000., bkgdatm='H2', broadf=True, remove_original_hdf=True, gpu_transfer=True):
        """Molecular database for Exomol form.

        Args:
           path: path for Exomol data directory/tag. For instance, "/home/CO/12C-16O/Li2015"
           nurange: wavenumber range list (cm-1) [min,max] or wavenumber grid
           margin: margin for nurange (cm-1)
           crit: line strength lower limit for extraction
           Ttyp: typical temperature to calculate Sij(T) used in crit
           bkgdatm: background atmosphere for broadening. e.g. H2, He,
           broadf: if False, the default broadening parameters in .def file is used
           remove_original_hdf: if True, the hdf5 and yaml files created while reading the original transition file(s) will be removed since those files will not be used after that.
           gpu_transfer: tranfer data to jnp.array? 

        Note:
           The trans/states files can be very large. For the first time to read it, we convert it to HDF/vaex. After the second-time, we use the HDF5 format with vaex instead.
        """
        self.path = pathlib.Path(path).expanduser()
        t0 = self.path.parents[0].stem
        molec = t0+'__'+str(self.path.stem)
        self.bkgdatm = bkgdatm
        print('Background atmosphere: ', self.bkgdatm)
        molecbroad = t0+'__'+self.bkgdatm

        self.crit = crit
        self.Ttyp = Ttyp
        self.margin = margin
        self.nurange = [np.min(nurange), np.max(nurange)]
        self.broadf = broadf
        self.states_file = self.path/pathlib.Path(molec+'.states.bz2')
        self.pf_file = self.path/pathlib.Path(molec+'.pf')
        self.def_file = self.path/pathlib.Path(molec+'.def')
        self.broad_file = self.path/pathlib.Path(molecbroad+'.broad')

        if not self.def_file.exists():
            self.download(molec, extension=['.def'])
        if not self.pf_file.exists():
            self.download(molec, extension=['.pf'])
        if not self.states_file.exists():
            self.download(molec, extension=['.states.bz2'])
        if not self.broad_file.exists():
            self.download(molec, extension=['.broad'])
            
        # load def
        self.n_Texp_def, self.alpha_ref_def, self.molmass, numinf, numtag = exomolapi.read_def(
            self.def_file)

        #  default n_Texp value if not given
        if self.n_Texp_def is None:
            self.n_Texp_def = 0.5
        #  default alpha_ref value if not given
        if self.alpha_ref_def is None:
            self.alpha_ref_def = 0.07

        # load states
        if self.states_file.with_suffix('.bz2.hdf5').exists():
            states = vaex.open(self.states_file.with_suffix('.bz2.hdf5'))
            ndstates = vaex.array_types.to_numpy(states)
        else:
            print(explanation_states)
            states = exomolapi.read_states(self.states_file)
            ndstates = vaex.array_types.to_numpy(states)

        # load pf
        pf = exomolapi.read_pf(self.pf_file)
        self.gQT = jnp.array(pf['QT'].to_numpy())  # grid QT
        self.T_gQT = jnp.array(pf['T'].to_numpy())  # T forgrid QT
        self.QTref = np.array(self.QT_interp(Tref))
        self.QTtyp = np.array(self.QT_interp(self.Ttyp))

        # trans file(s)
        print('Reading transition file')
        mask_needed = False
        if numinf is None:
            self.trans_file = self.path/pathlib.Path(molec+'.trans.bz2')
            if not self.trans_file.with_suffix('.hdf5').exists() and not self.trans_file.exists():
                self.download(molec, ['.trans.bz2'])

            if self.trans_file.with_suffix('.hdf5').exists():
                trans = vaex.open(self.trans_file.with_suffix('.hdf5'))
                cdt = (trans.nu_lines > self.nurange[0]-self.margin) \
                    * (trans.nu_lines < self.nurange[1]+self.margin)
                if not '_elower' in trans:
                    print(warning_old_exojax,
                          self.trans_file.with_suffix('.hdf5'))
                    return

                cdt = cdt * (self.get_Sij_typ(trans.Sij0,trans._elower, trans.nu_lines) > self.crit)
                trans = trans[cdt]
                ndtrans = vaex.array_types.to_numpy(trans)
            else:
                print(explanation_trans)
                trans = exomolapi.read_trans(self.trans_file)
                ndtrans = vaex.array_types.to_numpy(trans)
                mask_needed = True

            # compute gup and elower
            self._A, self.nu_lines, self._elower, self._gpp, self._jlower, self._jupper, mask_zeronu = exomolapi.pickup_gE(
                ndstates, ndtrans, self.trans_file)

            if self.trans_file.with_suffix('.hdf5').exists():
                self.Sij0 = ndtrans[:, 4]
            else:
                # Line strength: input should be ndarray not jnp array
                self.Sij0 = exomol.Sij0(
                    self._A, self._gpp, self.nu_lines, self._elower, self.QTref)
                self.Sij_typ = self.Sij0 * self.QTref / self.QTtyp \
                    * np.exp(-hcperk*self._elower * (1./self.Ttyp - 1./Tref)) \
                    * np.expm1(-hcperk*self.nu_lines/self.Ttyp) / np.expm1(-hcperk*self.nu_lines/Tref)

                # exclude the lines whose nu_lines evaluated inside exomolapi.pickup_gE (thus sometimes different from the "nu_lines" column in trans) is not positive
                trans['nu_positive'] = mask_zeronu
                trans = trans[trans.nu_positive].extract()
                trans.drop('nu_positive', inplace=True)
                trans['nu_lines'] = self.nu_lines
                trans['Sij0'] = self.Sij0
                trans['_elower'] = self._elower
                trans.export(self.trans_file.with_suffix('.hdf5'))

                if remove_original_hdf:
                    # remove the hdf5 and yaml files created while reading the original transition file.
                    if(self.trans_file.with_suffix('.bz2.hdf5').exists()):
                        os.remove(self.trans_file.with_suffix('.bz2.hdf5'))
                    if(self.trans_file.with_suffix('.bz2.yaml').exists()):
                        os.remove(self.trans_file.with_suffix('.bz2.yaml'))
        else:
            imin = np.searchsorted(
                numinf, self.nurange[0], side='right')-1  # left side
            imax = np.searchsorted(
                numinf, self.nurange[1], side='right')-1  # left side
            self.trans_file = []
            for k, i in enumerate(range(imin, imax+1)):
                trans_file = self.path / \
                    pathlib.Path(molec+'__'+numtag[i]+'.trans.bz2')
                if not trans_file.with_suffix('.hdf5').exists() and not trans_file.exists():
                    self.download(molec, extension=[
                                  '.trans.bz2'], numtag=numtag[i])

                if trans_file.with_suffix('.hdf5').exists():
                    trans = vaex.open(trans_file.with_suffix('.hdf5'))
                    cdt = (trans.nu_lines > self.nurange[0]-self.margin) \
                        * (trans.nu_lines < self.nurange[1]+self.margin)
                    if not '_elower' in trans:
                        print(warning_old_exojax,
                              trans_file.with_suffix('.hdf5'))
                        return

                    cdt = cdt * (self.get_Sij_typ(trans.Sij0,
                                 trans._elower, trans.nu_lines) > self.crit)
                    trans = trans[cdt]
                    ndtrans = vaex.array_types.to_numpy(trans)
                    self.trans_file.append(trans_file)
                else:
                    print(explanation_trans)
                    trans = exomolapi.read_trans(trans_file)
                    ndtrans = vaex.array_types.to_numpy(trans)
                    self.trans_file.append(trans_file)
                    mask_needed = True

                # compute gup and elower
                if k == 0:
                    self._A, self.nu_lines, self._elower, self._gpp, self._jlower, self._jupper, mask_zeronu = exomolapi.pickup_gE(
                        ndstates, ndtrans, trans_file)
                    if trans_file.with_suffix('.hdf5').exists():
                        self.Sij0 = ndtrans[:, 4]
                    else:
                        self.Sij0 = exomol.Sij0(
                            self._A, self._gpp, self.nu_lines, self._elower, self.QTref)
                        # exclude the lines whose nu_lines evaluated inside exomolapi.pickup_gE (thus sometimes different from the "nu_lines" column in trans) is not positive
                        trans['nu_positive'] = mask_zeronu
                        trans = trans[trans.nu_positive].extract()
                        trans.drop('nu_positive', inplace=True)
                        trans['nu_lines'] = self.nu_lines
                        trans['Sij0'] = self.Sij0
                        trans['_elower'] = self._elower

                    self.Sij_typ = self.get_Sij_typ(
                        self.Sij0, self._elower, self.nu_lines)
                else:
                    Ax, nulx, elowerx, gppx, jlowerx, jupperx, mask_zeronu = exomolapi.pickup_gE(
                        ndstates, ndtrans, trans_file)
                    if trans_file.with_suffix('.hdf5').exists():
                        Sij0x = ndtrans[:, 4]
                    else:
                        Sij0x = exomol.Sij0(
                            Ax, gppx, nulx, elowerx, self.QTref)
                        # exclude the lines whose nu_lines evaluated inside exomolapi.pickup_gE (thus sometimes different from the "nu_lines" column in trans) is not positive
                        trans['nu_positive'] = mask_zeronu
                        trans = trans[trans.nu_positive].extract()
                        trans.drop('nu_positive', inplace=True)
                        trans['nu_lines'] = nulx
                        trans['Sij0'] = Sij0x
                        trans['_elower'] = elowerx

                    Sij_typx = self.get_Sij_typ(Sij0x, elowerx, nulx)

                    self._A = np.hstack([self._A, Ax])
                    self.nu_lines = np.hstack([self.nu_lines, nulx])
                    self._elower = np.hstack([self._elower, elowerx])
                    self._gpp = np.hstack([self._gpp, gppx])
                    self._jlower = np.hstack([self._jlower, jlowerx])
                    self._jupper = np.hstack([self._jupper, jupperx])
                    self.Sij0 = np.hstack([self.Sij0, Sij0x])
                    self.Sij_typ = np.hstack([self.Sij_typ, Sij_typx])

                if not trans_file.with_suffix('.hdf5').exists():
                    trans.export(trans_file.with_suffix('.hdf5'))

                if remove_original_hdf:
                    # remove the hdf5 and yaml files created while reading the original transition file.
                    if(trans_file.with_suffix('.bz2.hdf5').exists()):
                        os.remove(trans_file.with_suffix('.bz2.hdf5'))
                    if(trans_file.with_suffix('.bz2.yaml').exists()):
                        os.remove(trans_file.with_suffix('.bz2.yaml'))

        if mask_needed:
            mask = (self.nu_lines > self.nurange[0]-self.margin)\
                * (self.nu_lines < self.nurange[1]+self.margin)\
                * (self.Sij_typ > self.crit)
        else:
            # define all true list just in case
            mask = np.ones_like(self.nu_lines, dtype=bool)
        self.masking(mask)
        self.set_broadening()  # Broadening parameters
        if gpu_transfer:
            self.generate_jnp_arrays()
        
    def get_Sij_typ(self, Sij0_in, elower_in, nu_in):
        """compute Sij at typical temperature self.Ttyp.

        Args:
           Sij0_in : line strength at Tref
           elower_in: elower
           nu_in: wavenumber bin

        Returns:
           Sij at Ttyp
        """
        return Sij0_in * self.QTref / self.QTtyp \
            * np.exp(-hcperk*elower_in * (1./self.Ttyp - 1./Tref)) \
            * np.expm1(-hcperk*nu_in/self.Ttyp) / np.expm1(-hcperk*nu_in/Tref)

    def masking(self, mask):
        """applying mask.
        Args:
           mask: mask to be applied. self.mask is updated.
        """
        # numpy float 64 Do not convert them jnp array
        self.nu_lines = self.nu_lines[mask]
        self.Sij0 = self.Sij0[mask]
        self._A = self._A[mask]
        self._elower = self._elower[mask]
        self._gpp = self._gpp[mask]
        self._jlower = self._jlower[mask]
        self._jupper = self._jupper[mask]


    def set_broadening(self, alpha_ref_def=None, n_Texp_def=None):
        """setting broadening parameters.

        Args:
           alpha_ref: set default alpha_ref and apply it. None=use self.alpha_ref_def
           n_Texp_def: set default n_Texp and apply it. None=use self.n_Texp_def
        """
        if alpha_ref_def:
            self.alpha_ref_def = alpha_ref_def
        if n_Texp_def:
            self.n_Texp_def = n_Texp_def

        if self.broadf:
            try:
                print('.broad is used.')
                bdat = exomolapi.read_broad(self.broad_file)
                codelv = exomolapi.check_bdat(bdat)
                print('Broadening code level=', codelv)
                if codelv == 'a0':
                    j2alpha_ref, j2n_Texp = exomolapi.make_j2b(bdat,
                                                               alpha_ref_default=self.alpha_ref_def,
                                                               n_Texp_default=self.n_Texp_def,
                                                               jlower_max=np.max(self._jlower))
                    self._alpha_ref = np.array(j2alpha_ref[self._jlower])
                    self._n_Texp = np.array(j2n_Texp[self._jlower])
                elif codelv == 'a1':
                    j2alpha_ref, j2n_Texp = exomolapi.make_j2b(bdat,
                                                               alpha_ref_default=self.alpha_ref_def,
                                                               n_Texp_default=self.n_Texp_def,
                                                               jlower_max=np.max(self._jlower))
                    jj2alpha_ref, jj2n_Texp = exomolapi.make_jj2b(bdat,
                                                                  j2alpha_ref_def=j2alpha_ref, j2n_Texp_def=j2n_Texp,
                                                                  jupper_max=np.max(self._jupper))
                    self._alpha_ref = np.array(
                        jj2alpha_ref[self._jlower, self._jupper])
                    self._n_Texp = np.array(
                        jj2n_Texp[self._jlower, self._jupper])
            except:
                print(
                    'Warning: Cannot load .broad. The default broadening parameters are used.')
                self._alpha_ref = np.array(
                    self.alpha_ref_def*np.ones_like(self._jlower))
                self._n_Texp = np.array(
                    self.n_Texp_def*np.ones_like(self._jlower))

        else:
            print('The default broadening parameters are used.')
            self._alpha_ref = np.array(
                self.alpha_ref_def*np.ones_like(self._jlower))
            self._n_Texp = np.array(self.n_Texp_def*np.ones_like(self._jlower))
            
    def generate_jnp_arrays(self):
        """(re)generate jnp.arrays.

        Note:
           We have nd arrays and jnp arrays. We usually apply the mask to nd arrays and then generate jnp array from the corresponding nd array. For instance, self._A is nd array and self.A is jnp array.

        """

        # jnp arrays
        self.dev_nu_lines = jnp.array(self.nu_lines)
        self.logsij0 = jnp.array(np.log(self.Sij0))
        self.A = jnp.array(self._A)
        self.gamma_natural = gn(self.A)
        self.elower = jnp.array(self._elower)
        self.gpp = jnp.array(self._gpp)
        self.jlower = jnp.array(self._jlower, dtype=int)
        self.jupper = jnp.array(self._jupper, dtype=int)
        self.alpha_ref = jnp.array(self._alpha_ref)
        self.n_Texp = jnp.array(self._n_Texp)
    
    def QT_interp(self, T):
        """interpolated partition function.

        Args:
           T: temperature

        Returns:
           Q(T) interpolated in jnp.array
        """
        return jnp.interp(T, self.T_gQT, self.gQT)

    def qr_interp(self, T):
        """interpolated partition function ratio.

        Args:
           T: temperature

        Returns:
           qr(T)=Q(T)/Q(Tref) interpolated in jnp.array
        """
        return self.QT_interp(T)/self.QT_interp(Tref)

    def download(self, molec, extension, numtag=None):
        """Downloading Exomol files.

        Args:
           molec: like "12C-16O__Li2015"
           extension: extension list e.g. [".pf",".def",".trans.bz2",".states.bz2",".broad"]
           numtag: number tag of transition file if exists. e.g. "11100-11200"

        Note:
           The download URL is written in exojax.utils.url.
        """
        import urllib.request
        from exojax.utils.molname import e2s
        from exojax.utils.url import url_ExoMol

        tag = molec.split('__')
        molname_simple = e2s(tag[0])

        for ext in extension:
            if ext == '.trans.bz2' and numtag is not None:
                ext = '__'+numtag+ext
            if ext == '.broad':
                pfname_arr = [tag[0]+'__H2'+ext, tag[0] +
                              '__He'+ext, tag[0]+'__air'+ext]
                url = url_ExoMol()+molname_simple+'/'+tag[0]+'/'
            else:
                pfname_arr = [molec+ext]
                url = url_ExoMol()+molname_simple+'/'+tag[0]+'/'+tag[1]+'/'
            for pfname in pfname_arr:
                pfpath = url+pfname
                os.makedirs(str(self.path), exist_ok=True)
                print('Downloading '+pfpath)
                try:
                    urllib.request.urlretrieve(pfpath, str(self.path/pfname))
                except:
                    print("Error: Couldn't download "+ext+' file and save.')


class MdbHit(object):
    """molecular database of HITRAN 2012 / HITEMP 2020.

    Attributes:
        nurange: nu range [min,max] (cm-1)
        nu_lines (nd array): line center (cm-1)
        Sij0 (nd array): line strength at T=Tref (cm)
        dev_nu_lines (jnp array): line center in device (cm-1)
        logsij0 (jnp array): log line strength at T=Tref
        A (jnp array): Einstein A coeeficient
        gamma_natural (jnp array): gamma factor of the natural broadening
        gamma_air (jnp array): gamma factor of air pressure broadening
        gamma_self (jnp array): gamma factor of self pressure broadening
        elower (jnp array): the lower state energy (cm-1)
        gpp (jnp array): statistical weight
        n_air (jnp array): air temperature exponent
    """

    def __init__(self, path, nurange=[-np.inf, np.inf], margin=0.0, crit=0., Ttyp=1000., extract=False, gpu_transfer=True):
        """Molecular database for HITRAN/HITEMP form.

        Args:
           path: path for HITRAN/HITEMP par file
           nurange: wavenumber range list (cm-1) [min,max] or wavenumber grid
           margin: margin for nurange (cm-1)
           crit: line strength lower limit for extraction
           Ttyp: typical temperature to calculate Sij(T) used in crit
           extract: If True, it extracts the opacity having the wavenumber between nurange +- margin. Use when you want to reduce the memory use.
           gpu_transfer: tranfer data to jnp.array? 
        """
        from exojax.spec.hitran import SijT
        if ("hit" in path and path[-4:] == ".bz2"):
            path = path[:-4]
            print('Warning: path changed (.bz2 removed):', path)
        if ("HITEMP" in path and path[-4:] == ".par"):
            path = path + '.bz2'
            print('Warning: path changed (.bz2 added):', path)

        self.path = pathlib.Path(path).expanduser()
        numinf, numtag = hitranapi.read_path(self.path)
        self.crit = crit
        self.Ttyp = Ttyp
        self.margin = margin
        self.nurange = [np.min(nurange), np.max(nurange)]

        if numinf is None:
            if not self.path.exists():
                self.download()

            if extract:
                if self.path.suffix == '.bz2':
                    tag = str(nurange[0])+'_'+str(nurange[-1])+'_'+str(margin)
                    self.path = hitranapi.extract_hitemp(
                        str(self.path), nurange, margin, tag)
                    print('self.path changed:', self.path)
                else:
                    print(
                        'Warning: "extract" option is available only for .bz2 format. No "extract" applied')

            if self.path.suffix == '.bz2':
                import bz2
                import shutil
                if self.path.with_suffix('').exists():
                    os.remove(self.path.with_suffix(''))
                print('bunziping')
                with bz2.BZ2File(str(self.path)) as fr:
                    with open(str(self.path.with_suffix('')), 'wb') as fw:
                        shutil.copyfileobj(fr, fw)
                self.path = self.path.with_suffix('')

            os.makedirs(str(self.path.parent), exist_ok=True)
            hapi.db_begin(str(self.path.parent))
            molec = str(self.path.stem)
            self.molecid = search_molecid(molec)
            self.get_value_hapi(molec)
        else:
            molnm = str(self.path.name)[0:2]
            if molnm == '01' or molnm == '02':
                if self.path.name != molnm+'_HITEMP2010':
                    path_old = self.path
                    self.path = self.path.parent/str(molnm+'_HITEMP2010')
                    print('Warning: Changed the line list path from',
                          path_old, 'to', self.path)

            imin = np.searchsorted(
                numinf, self.nurange[0], side='right')-1  # left side
            imax = np.searchsorted(
                numinf, self.nurange[1], side='right')-1  # left side
            for k, i in enumerate(range(imin, imax+1)):
                flname = pathlib.Path(molnm+'_'+numtag[i]+'_HITEMP2010.par')
                sub_file = self.path/numtag[i]/flname
                if not sub_file.exists():
                    self.download(numtag=numtag[i])

                os.makedirs(str(self.path.parent), exist_ok=True)
                hapi.db_begin(str(self.path/numtag[i]))
                molec = str(flname.stem)
                self.molecid = search_molecid(molec)
                if k == 0:
                    self.get_value_hapi(molec)
                else:
                    self.update_value_hapi(molec)

        self.logsij0 = jnp.array(np.log(self.Sij0))
        self.elower = jnp.array(self._elower)

        # get pf
        self.gQT, self.T_gQT = hitranapi.get_pf(self.molecid, self.uniqiso)
        self.QTtyp = self.Qr_layer_HAPI([self.Ttyp])[0]
        self.Sij_typ = SijT(self.Ttyp, self.logsij0,
                            self.nu_lines, self.elower, self.QTtyp)

        mask = (self.nu_lines > self.nurange[0]-self.margin)\
            * (self.nu_lines < self.nurange[1]+self.margin)\
            * (self.Sij_typ > self.crit)
        self.masking(mask)
        if gpu_transfer:
            self.generate_jnp_arrays()
        
    def get_value_hapi(self, molec):
        """get values using HAPI.

        Args:
           molec: molecular name
        """
        self.nu_lines = hapi.getColumn(molec, 'nu')
        self.Sij0 = hapi.getColumn(molec, 'sw')
        self.delta_air = hapi.getColumn(molec, 'delta_air')
        self.isoid = hapi.getColumn(molec, 'local_iso_id')
        self.uniqiso = np.unique(self.isoid)
        self._A = hapi.getColumn(molec, 'a')
        self._n_air = hapi.getColumn(molec, 'n_air')
        self._gamma_air = hapi.getColumn(molec, 'gamma_air')
        self._gamma_self = hapi.getColumn(molec, 'gamma_self')
        self._elower = hapi.getColumn(molec, 'elower')
        self._gpp = hapi.getColumn(molec, 'gpp')

    def update_value_hapi(self, molec):
        """update values using HAPI.

        Args:
           molec: molecular name
        """
        self.nu_lines = np.hstack([self.nu_lines, hapi.getColumn(molec, 'nu')])
        self.Sij0 = np.hstack([self.Sij0, hapi.getColumn(molec, 'sw')])
        self.delta_air = np.hstack(
            [self.delta_air, hapi.getColumn(molec, 'delta_air')])
        self.isoid = np.hstack(
            [self.isoid, hapi.getColumn(molec, 'local_iso_id')])
        self.uniqisox = np.unique(self.isoid)
        self._A = np.hstack([self._A, hapi.getColumn(molec, 'a')])
        self._n_air = np.hstack([self._n_air, hapi.getColumn(molec, 'n_air')])
        self._gamma_air = np.hstack(
            [self._gamma_air, hapi.getColumn(molec, 'gamma_air')])
        self._gamma_self = np.hstack(
            [self._gamma_self, hapi.getColumn(molec, 'gamma_self')])
        self._elower = np.hstack(
            [self._elower, hapi.getColumn(molec, 'elower')])
        self._gpp = np.hstack([self._gpp, hapi.getColumn(molec, 'gpp')])

    def masking(self, mask):
        """applying mask and (re)generate jnp.arrays.

        Args:
           mask: mask to be applied

        Note:
           We have nd arrays and jnp arrays. We apply the mask to nd arrays and generate jnp array from the corresponding nd array. For instance, self._A is nd array and self.A is jnp array.
        """

        # numpy float 64 Do not convert them jnp array
        self.nu_lines = self.nu_lines[mask]
        self.Sij0 = self.Sij0[mask]
        self.delta_air = self.delta_air[mask]
        self.isoid = self.isoid[mask]
        self.uniqiso = np.unique(self.isoid)

        # numpy float 64 copy source for jnp
        self._A = self._A[mask]
        self._n_air = self._n_air[mask]
        self._gamma_air = self._gamma_air[mask]
        self._gamma_self = self._gamma_self[mask]
        self._elower = self._elower[mask]
        self._gpp = self._gpp[mask]

    def generate_jnp_arrays(self):
        """(re)generate jnp.arrays.
        
        Note:
           We have nd arrays and jnp arrays. We usually apply the mask to nd arrays and then generate jnp array from the corresponding nd array. For instance, self._A is nd array and self.A is jnp array.
        
        """
        # jnp.array copy from the copy sources
        self.dev_nu_lines = jnp.array(self.nu_lines)
        self.logsij0 = jnp.array(np.log(self.Sij0))
        self.A = jnp.array(self._A)
        self.n_air = jnp.array(self._n_air)
        self.gamma_air = jnp.array(self._gamma_air)
        self.gamma_self = jnp.array(self._gamma_self)
        self.elower = jnp.array(self._elower)
        self.gpp = jnp.array(self._gpp)
        self.gamma_natural = gn(self.A)

    def download(self, numtag=None):
        """Downloading HITRAN 2012/HITEMP 2020 par file.

        Note:
           The download URL is written in exojax.utils.url.
        """
        import urllib.request
        from exojax.utils.url import url_HITRAN12
        from exojax.utils.url import url_HITEMP
        from exojax.utils.url import url_HITEMP10
        import shutil

        os.makedirs(str(self.path.parent), exist_ok=True)
        try:
            url = url_HITRAN12()+self.path.name
            urllib.request.urlretrieve(url, str(self.path))
        except:
            print(url)
            print('HITRAN download failed')
        try:
            url = url_HITEMP()+self.path.name
            urllib.request.urlretrieve(url, str(self.path))
        except:
            print(url)
            print('HITEMP download failed')

        if numtag is not None:
            molnm = str(self.path.name)[0:2]
            if molnm == '01':
                os.makedirs(str(self.path/numtag), exist_ok=True)
                dldir = 'H2O_line_list/'
            if molnm == '02':
                os.makedirs(str(self.path/numtag), exist_ok=True)
                dldir = 'CO2_line_list/'
            flname = molnm+'_'+numtag+'_HITEMP2010.zip'
            try:
                url = url_HITEMP10()+dldir+flname
                urllib.request.urlretrieve(url, str(self.path/numtag/flname))
            except:
                print(url)
                print('HITEMP2010 download failed')
            else:
                print('HITEMP2010 download succeeded')
                shutil.unpack_archive(
                    self.path/numtag/flname, self.path/numtag)

                imin = int(numtag[0:5])
                imax = int(numtag[6:11])
                numtag_nonzero = str(imin)+'-'+str(imax)
                # unzipping results in non-zero-fill filename
                # ex."02_6500-12785_HITEMP2010.par", not "02_'0'6500-12785_HITEMP2010.par"
                # so need to rename the file
                flname_nonzero = molnm+'_'+numtag_nonzero+'_HITEMP2010.par'
                flname_zero = molnm+'_'+numtag+'_HITEMP2010.par'
                if (self.path/numtag/flname_nonzero).exists():
                    if flname_zero != flname_nonzero:
                        os.rename(self.path/numtag/flname_nonzero,
                                  self.path/numtag/flname_zero)
                        print('renamed par file in', self.path/pathlib.Path(numtag),
                              ':', flname_nonzero, '=>', flname_zero)

    def ExomolQT(self, path):
        """use a partition function from ExoMol.

        Args:
           path: path for Exomol data directory/tag. For instance, "/home/CO/12C-16O/Li2015"
        """
        # load pf
        self.empath = pathlib.Path(path).expanduser()
        t0 = self.empath.parents[0].stem
        molec = t0+'__'+str(self.empath.stem)
        self.pf_file = self.empath/pathlib.Path(molec+'.pf')
        if not self.pf_file.exists():
            self.exomol_pf_download(molec)

        pf = exomolapi.read_pf(self.pf_file)
        self.gQT = jnp.array(pf['QT'].to_numpy())  # grid QT
        self.T_gQT = jnp.array(pf['T'].to_numpy())  # T forgrid QT

    def exomol_pf_download(self, molec):
        """Downloading Exomol pf files.

        Args:
           molec: like "12C-16O__Li2015"

        Note:
           The download URL is written in exojax.utils.url.
        """
        import urllib.request
        from exojax.utils.molname import e2s
        from exojax.utils.url import url_ExoMol

        tag = molec.split('__')
        molname_simple = e2s(tag[0])
        url = url_ExoMol()+molname_simple+'/'+tag[0]+'/'+tag[1]+'/'

        ext = '.pf'
        pfname = molec+ext
        pfpath = url+pfname
        os.makedirs(str(self.empath), exist_ok=True)
        print('Downloading '+pfpath)
        try:
            urllib.request.urlretrieve(pfpath, str(self.empath/pfname))
        except:
            print("Error: Couldn't download "+ext+' file and save.')

    def QT_iso_interp(self, idx, T):
        """interpolated partition function.

        Args:
           idx: index for HITRAN isotopologue number
           T: temperature

        Returns:
           Q(idx, T) interpolated in jnp.array
        """
        return jnp.interp(T, self.T_gQT[idx], self.gQT[idx])

    def qr_iso_interp(self, idx, T):
        """interpolated partition function ratio.

        Args:
           idx: index for HITRAN isotopologue number
           T: temperature

        Returns:
           qr(T)=Q(T)/Q(Tref) interpolated in jnp.array
        """
        return self.QT_iso_interp(idx, T)/self.QT_iso_interp(idx, Tref)

    def Qr_HAPI(self, Tarr):
        """Partition Function ratio using HAPI partition data.

        Args:
           Tarr: temperature array (K)

        Returns:
           Qr = partition function ratio array [N_Tarr x N_iso]

        Note:
           N_Tarr = len(Tarr), N_iso = len(self.uniqiso)
        """
        allT = list(np.concatenate([[Tref], Tarr]))
        Qrx = []
        for idx, iso in enumerate(self.uniqiso):
            Tmin, Tmax = hapi.get_TMIN_TMAX_FOR_BD_TIPS_2017_PYTHON(self.molecid, iso)
            if(max(allT) > Tmax):
                raise ValueError('%.1f K is outside the supported temperature range of the HITRAN partition function data for isotope #%d of molecule #%d (%.1f -- %.1f K)' % (max(allT), iso, self.molecid, Tmin, Tmax))
            if(min(allT) < Tmin):
                raise ValueError('%.1f K is outside the supported temperature range of the HITRAN partition function data for isotope #%d of molecule #%d (%.1f -- %.1f K)' % (min(allT), iso, self.molecid, Tmin, Tmax))

            Qrx_iso = jit(vmap(self.QT_iso_interp, (None, 0)))(idx, jnp.array(allT))
            Qrx.append(Qrx_iso)
        Qrx = np.array(Qrx)
        qr = Qrx[:, 1:].T/Qrx[:, 0]  # Q(T)/Q(Tref)
        return qr

    def Qr_line_HAPI(self, T):
        """Partition Function ratio using HAPI partition data.

        Args:
           T: temperature (K)

        Returns:
           Qr_line, partition function ratio array for lines [Nlines]

        Note:
           Nlines=len(self.nu_lines)
        """
        qr_line = np.ones_like(self.isoid, dtype=np.float64)
        qrx = self.Qr_HAPI([T])
        for idx, iso in enumerate(self.uniqiso):
            mask = self.isoid == iso
            qr_line[mask] = qrx[0, idx]
        return qr_line

    def Qr_line_HAPI_jax(self, T):
        """Partition Function ratio using HAPI partition data.
        (This function works for JAX environment.)

        Args:
           T: temperature (K)

        Returns:
           Qr_line, partition function ratio array for lines [Nlines]

        Note:
           Nlines=len(self.nu_lines)
        """
        qrx = []
        for idx, iso in enumerate(self.uniqiso):
            qrx.append(self.qr_iso_interp(idx, T))

        qr_line = jnp.zeros(len(self.isoid))
        for idx, iso in enumerate(self.uniqiso):
            mask_idx = np.where(self.isoid == iso)
            qr_line = qr_line.at[jnp.index_exp[mask_idx]].set(qrx[idx])
        return qr_line

    def Qr_layer_HAPI(self, Tarr):
        """Partition Function ratio using HAPI partition sum.

        Args:
           Tarr: temperature array (K)

        Returns:
           Qr_layer, partition function ratio array for lines [N_Tarr x Nlines]

        Note:
           Nlines=len(self.nu_lines)
           N_Tarr=len(Tarr)
        """
        NP = len(Tarr)
        qt = np.zeros((NP, len(self.isoid)))
        qr = self.Qr_HAPI(Tarr)
        for idx, iso in enumerate(self.uniqiso):
            mask = self.isoid == iso
            for ilayer in range(NP):
                qt[ilayer, mask] = qr[ilayer, idx]
        return qt


def search_molecid(molec):
    """molec id from molec (source table name) of HITRAN/HITEMP.

    Args:
       molec: source table name

    Return:
       int: molecid (HITRAN molecular id)
    """
    try:
        hitf = molec.split('_')
        molecid = int(hitf[0])
        return molecid
    except:
        raise ValueError('Define molecid by yourself.')

class AdbVald(object):
    """atomic database from VALD3 (http://vald.astro.uu.se/)

    AdbVald is a class for VALD3.

    Attributes:
        nurange: nu range [min,max] (cm-1)
        nu_lines (nd array):      line center (cm-1) (#NOT frequency in (s-1))
        dev_nu_lines (jnp array): line center (cm-1) in device
        Sij0 (nd array): line strength at T=Tref (cm)
        logsij0 (jnp array): log line strength at T=Tref
        A (jnp array): Einstein A coeeficient in (s-1)
        elower (jnp array): the lower state energy (cm-1)
        eupper (jnp array): the upper state energy (cm-1)
        gupper: (jnp array): upper statistical weight
        jlower (jnp array): lower J (rotational quantum number, total angular momentum)
        jupper (jnp array): upper J
        QTmask (jnp array): identifier of species for Q(T)
        ielem (jnp array):  atomic number (e.g., Fe=26)
        iion (jnp array):  ionized level (e.g., neutral=1, singly ionized=2, etc.)
        solarA (jnp array): solar abundance (log10 of number density in the Sun)
        atomicmass (jnp array): atomic mass (amu)
        ionE (jnp array): ionization potential (eV)
        gamRad (jnp array): log of gamma of radiation damping (s-1) #(https://www.astro.uu.se/valdwiki/Vald3Format)
        gamSta (jnp array): log of gamma of Stark damping (s-1)
        vdWdamp (jnp array):  log of (van der Waals damping constant / neutral hydrogen number) (s-1)

        Note:
           For the first time to read the VALD line list, it is converted to HDF/vaex. After the second-time, we use the HDF5 format with vaex instead.
    """

    def __init__(self, path, nurange=[-np.inf, np.inf], margin=0.0, crit=0., Irwin=False, gpu_transfer=True):
        """Atomic database for VALD3 "Long format".

        Args:
          path: path for linelists downloaded from VALD3 with a query of "Long format" in the format of "Extract All", "Extract Stellar", or "Extract Element"
          nurange: wavenumber range list (cm-1) or wavenumber array
          margin: margin for nurange (cm-1)
          crit: line strength lower limit for extraction
          Irwin: if True(1), the partition functions of Irwin1981 is used, otherwise those of Barklem&Collet2016
          gpu_transfer: tranfer data to jnp.array? 

        Note:
          (written with reference to moldb.py, but without using feather format)
        """

        # load args
        self.vald3_file = pathlib.Path(path).expanduser()  # VALD3 output
        self.nurange = [np.min(nurange), np.max(nurange)]
        self.margin = margin
        self.crit = crit

        # load vald file
        print('Reading VALD file')
        if self.vald3_file.with_suffix('.hdf5').exists():
            valdd = vaex.open(self.vald3_file.with_suffix('.hdf5'))
        else:
            print(
                "Note: Couldn't find the hdf5 format. We convert data to the hdf5 format.")
            valdd = atomllapi.read_ExAll(self.vald3_file)  # vaex.DataFrame
        pvaldd = valdd.to_pandas_df()  # pandas.DataFrame

        # compute additional transition parameters
        self._A, self.nu_lines, self._elower, self._eupper, self._gupper, self._jlower, self._jupper, self._ielem, self._iion, self._gamRad, self._gamSta, self._vdWdamp = atomllapi.pickup_param(
            pvaldd)

        # load the partition functions (for 284 atomic species)
        pfTdat, self.pfdat = atomllapi.load_pf_Barklem2016()  # Barklem & Collet (2016)
        self.T_gQT = jnp.array(pfTdat.columns[1:], dtype=float)
        self.gQT_284species = jnp.array(self.pfdat.iloc[:, 1:].to_numpy(
            dtype=float))  # grid Q vs T vs Species
        self.QTref_284 = np.array(self.QT_interp_284(Tref))
        # identify index of QT grid (gQT) for each line
        self._QTmask = self.make_QTmask(self._ielem, self._iion)

        # Line strength: input shoud be ndarray not jnp array
        self.Sij0 = atomll.Sij0(self._A, self._gupper, self.nu_lines,
                                self._elower, self.QTref_284, self._QTmask, Irwin)  # 211013

        ### MASKING ###
        mask = (self.nu_lines > self.nurange[0]-self.margin)\
            * (self.nu_lines < self.nurange[1]+self.margin)\
            * (self.Sij0 > self.crit)

        self.masking(mask)
        if gpu_transfer:
            self.generate_jnp_arrays()
        
        # Compile atomic-specific data for each absorption line of interest
        ipccd = atomllapi.load_atomicdata()
        self.solarA = jnp.array(
            list(map(lambda x: ipccd[ipccd['ielem'] == x].iat[0, 4], self.ielem)))
        self.atomicmass = jnp.array(
            list(map(lambda x: ipccd[ipccd['ielem'] == x].iat[0, 5], self.ielem)))
        df_ionE = atomllapi.load_ionization_energies()
        self.ionE = jnp.array(
            list(map(atomllapi.pick_ionE, self.ielem, self.iion, [df_ionE, ] * len(self.ielem))))

    def masking(self, mask):
        """applying mask.

        Args:
           mask: mask to be applied. self.mask is updated.

        """
        # numpy float 64 Do not convert them jnp array
        self.nu_lines = self.nu_lines[mask]
        self.Sij0 = self.Sij0[mask]
        self._A = self._A[mask]
        self._elower = self._elower[mask]
        self._eupper = self._eupper[mask]
        self._gupper = self._gupper[mask]
        self._jlower = self._jlower[mask]
        self._jupper = self._jupper[mask]
        self._QTmask = self._QTmask[mask]
        self._ielem = self._ielem[mask]
        self._iion = self._iion[mask]
        self._gamRad = self._gamRad[mask]
        self._gamSta = self._gamSta[mask]
        self._vdWdamp = self._vdWdamp[mask]


    def generate_jnp_arrays(self):
        """(re)generate jnp.arrays.

        Note:
           We have nd arrays and jnp arrays. We usually apply the mask to nd arrays and then generate jnp array from the corresponding nd array. For instance, self._A is nd array and self.A is jnp array.

        """
        # jnp arrays
        self.dev_nu_lines = jnp.array(self.nu_lines)
        self.logsij0 = jnp.array(np.log(self.Sij0))
        self.A = jnp.array(self._A)
        self.elower = jnp.array(self._elower)
        self.eupper = jnp.array(self._eupper)
        self.gupper = jnp.array(self._gupper)
        self.jlower = jnp.array(self._jlower, dtype=int)
        self.jupper = jnp.array(self._jupper, dtype=int)

        self.QTmask = jnp.array(self._QTmask, dtype=int)
        self.ielem = jnp.array(self._ielem, dtype=int)
        self.iion = jnp.array(self._iion, dtype=int)
        self.gamRad = jnp.array(self._gamRad)
        self.gamSta = jnp.array(self._gamSta)
        self.vdWdamp = jnp.array(self._vdWdamp)

    def Atomic_gQT(self, atomspecies):
        """Select grid of partition function especially for the species of
        interest.

        Args:
            atomspecies: species e.g., "Fe 1", "Sr 2", etc.

        Returns:
            gQT: grid Q(T) for the species
        """
        atomspecies_Roman = atomspecies.split(
            ' ')[0] + '_' + 'I'*int(atomspecies.split(' ')[-1])
        gQT = self.gQT_284species[np.where(
            self.pfdat['T[K]'] == atomspecies_Roman)][0]
        return gQT

    def QT_interp(self, atomspecies, T):
        """interpolated partition function The partition functions of Barklem &
        Collet (2016) are adopted.

        Args:
          atomspecies: species e.g., "Fe 1"
          T: temperature

        Returns:
          Q(T): interpolated in jnp.array for the Atomic Species
        """
        gQT = self.Atomic_gQT(atomspecies)
        QT = jnp.interp(T, self.T_gQT, gQT)
        return QT

    def QT_interp_Irwin_Fe(self, T, atomspecies='Fe 1'):
        """interpolated partition function This function is for the exceptional
        case where you want to adopt partition functions of Irwin (1981) for Fe
        I (Other species are not yet implemented).

        Args:
          atomspecies: species e.g., "Fe 1"
          T: temperature

        Returns:
          Q(T): interpolated in jnp.array for the Atomic Species
        """
        gQT = self.Atomic_gQT(atomspecies)
        QT = atomllapi.partfn_Fe(T)
        return QT

    def qr_interp(self, atomspecies, T):
        """interpolated partition function ratio The partition functions of
        Barklem & Collet (2016) are adopted.

        Args:
           T: temperature
           atomspecies: species e.g., "Fe 1"

        Returns:
           qr(T)=Q(T)/Q(Tref): interpolated in jnp.array
        """
        return self.QT_interp(atomspecies, T)/self.QT_interp(atomspecies, Tref)

    def qr_interp_Irwin_Fe(self, T, atomspecies='Fe 1'):
        """interpolated partition function ratio This function is for the
        exceptional case where you want to adopt partition functions of Irwin
        (1981) for Fe I (Other species are not yet implemented).

        Args:
           T: temperature
           atomspecies: species e.g., "Fe 1"

        Returns:
           qr(T)=Q(T)/Q(Tref): interpolated in jnp.array
        """
        return self.QT_interp_Irwin_Fe(T, atomspecies)/self.QT_interp_Irwin_Fe(Tref, atomspecies)

    def QT_interp_284(self, T):
        """interpolated partition function of all 284 species.

        Args:
           T: temperature

        Returns:
           Q(T)*284: interpolated in jnp.array for all 284 Atomic Species
        """
        list_gQT_eachspecies = self.gQT_284species.tolist()
        listofDA_gQT_eachspecies = list(
            map(lambda x: jnp.array(x), list_gQT_eachspecies))
        listofQT = list(map(lambda x: jnp.interp(
            T, self.T_gQT, x), listofDA_gQT_eachspecies))
        QT_284 = jnp.array(listofQT)
        return QT_284

    def make_QTmask(self, ielem, iion):
        """Convert the species identifier to the index for Q(Tref) grid (gQT)
        for each line.

        Args:
            ielem:  atomic number (e.g., Fe=26)
            iion:  ionized level (e.g., neutral=1, singly ionized=2, etc.)

        Returns:
            QTmask_sp:  array of index of Q(Tref) grid (gQT) for each line
        """
        def species_to_QTmask(ielem, iion):
            sp_Roman = atomllapi.PeriodicTable[ielem] + '_' + 'I'*iion
            QTmask = np.where(self.pfdat['T[K]'] == sp_Roman)[0][0]
            return QTmask
        QTmask_sp = np.array(
            list(map(species_to_QTmask, ielem, iion))).astype('int')
        return QTmask_sp


class AdbSepVald(object):
    """atomic database from VALD3 with an additional axis for separating each
    species (atom or ion)

    AdbSepVald is a class for VALD3.

    Attributes:
        nu_lines (nd array):      line center (cm-1) (#NOT frequency in (s-1))
        dev_nu_lines (jnp array): line center (cm-1) in device
        logsij0 (jnp array): log line strength at T=Tref
        elower (jnp array): the lower state energy (cm-1)
        eupper (jnp array): the upper state energy (cm-1)
        QTmask (jnp array): identifier of species for Q(T)
        ielem (jnp array):  atomic number (e.g., Fe=26)
        iion (jnp array):  ionized level (e.g., neutral=1, singly ionized=2, etc.)
        atomicmass (jnp array): atomic mass (amu)
        ionE (jnp array): ionization potential (eV)
        gamRad (jnp array): log of gamma of radiation damping (s-1) #(https://www.astro.uu.se/valdwiki/Vald3Format)
        gamSta (jnp array): log of gamma of Stark damping (s-1)
        vdWdamp (jnp array):  log of (van der Waals damping constant / neutral hydrogen number) (s-1)
        uspecies (jnp array): unique combinations of ielem and iion [N_species x 2(ielem and iion)]
        N_usp (int): number of species (atoms and ions)
        L_max (int): maximum number of spectral lines for a single species
        gQT_284species (jnp array): partition function grid of 284 species
        T_gQT (jnp array): temperatures in the partition function grid
    """

    def __init__(self, adb):
        """Species-separated atomic database for VALD3.

        Args:
            adb: adb instance made by the AdbVald class, which stores the lines of all species together

        """
        self.nu_lines = atomll.sep_arr_of_sp(
            adb.nu_lines, adb, trans_jnp=False)
        self.QTmask = atomll.sep_arr_of_sp(adb.QTmask, adb, inttype=True).T[0]

        self.ielem = atomll.sep_arr_of_sp(adb.ielem, adb, inttype=True).T[0]
        self.iion = atomll.sep_arr_of_sp(adb.iion, adb, inttype=True).T[0]
        self.atomicmass = atomll.sep_arr_of_sp(adb.atomicmass, adb).T[0]
        self.ionE = atomll.sep_arr_of_sp(adb.ionE, adb).T[0]

        self.logsij0 = atomll.sep_arr_of_sp(adb.logsij0, adb)
        self.dev_nu_lines = atomll.sep_arr_of_sp(adb.dev_nu_lines, adb)
        self.elower = atomll.sep_arr_of_sp(adb.elower, adb)
        self.eupper = atomll.sep_arr_of_sp(adb.eupper, adb)
        self.gamRad = atomll.sep_arr_of_sp(adb.gamRad, adb)
        self.gamSta = atomll.sep_arr_of_sp(adb.gamSta, adb)
        self.vdWdamp = atomll.sep_arr_of_sp(adb.vdWdamp, adb)

        self.uspecies = atomll.get_unique_species(adb)
        self.N_usp = len(self.uspecies)
        self.L_max = self.nu_lines.shape[1]

        self.gQT_284species = adb.gQT_284species
        self.T_gQT = adb.T_gQT


class AdbKurucz(object):
    """atomic database from Kurucz (http://kurucz.harvard.edu/linelists/)

    AdbKurucz is a class for Kurucz line list.

    Attributes:
        nurange: nu range [min,max] (cm-1)
        nu_lines (nd array):      line center (cm-1) (#NOT frequency in (s-1))
        dev_nu_lines (jnp array): line center (cm-1) in device
        Sij0 (nd array): line strength at T=Tref (cm)
        logsij0 (jnp array): log line strength at T=Tref
        A (jnp array): Einstein A coeeficient in (s-1)
        elower (jnp array): the lower state energy (cm-1)
        eupper (jnp array): the upper state energy (cm-1)
        gupper: (jnp array): upper statistical weight
        jlower (jnp array): lower J (rotational quantum number, total angular momentum)
        jupper (jnp array): upper J
        QTmask (jnp array): identifier of species for Q(T)
        ielem (jnp array):  atomic number (e.g., Fe=26)
        iion (jnp array):  ionized level (e.g., neutral=1, singly ionized=2, etc.)
        gamRad (jnp array): log of gamma of radiation damping (s-1) #(https://www.astro.uu.se/valdwiki/Vald3Format)
        gamSta (jnp array): log of gamma of Stark damping (s-1)
        vdWdamp (jnp array):  log of (van der Waals damping constant / neutral hydrogen number) (s-1)
    """

    def __init__(self, path, nurange=[-np.inf, np.inf], margin=0.0, crit=0., Irwin=False, gpu_transfer=True):
        """Atomic database for Kurucz line list "gf????.all".

        Args:
          path: path for linelists (gf????.all) downloaded from the Kurucz web page
          nurange: wavenumber range list (cm-1) or wavenumber array
          margin: margin for nurange (cm-1)
          crit: line strength lower limit for extraction
          Irwin: if True(1), the partition functions of Irwin1981 is used, otherwise those of Barklem&Collet2016
          gpu_transfer: tranfer data to jnp.array? 

        Note:
          (written with reference to moldb.py, but without using feather format)
        """

        # load args
        self.kurucz_file = pathlib.Path(path).expanduser()
        self.nurange = [np.min(nurange), np.max(nurange)]
        self.margin = margin
        self.crit = crit

        # load kurucz file
        print('Reading Kurucz file')
        self._A, self.nu_lines, self._elower, self._eupper, self._gupper, self._jlower, self._jupper, self._ielem, self._iion, self._gamRad, self._gamSta, self._vdWdamp = atomllapi.read_kurucz(
            self.kurucz_file)

        # load the partition functions (for 284 atomic species)
        pfTdat, self.pfdat = atomllapi.load_pf_Barklem2016()  # Barklem & Collet (2016)
        self.T_gQT = jnp.array(pfTdat.columns[1:], dtype=float)
        self.gQT_284species = jnp.array(self.pfdat.iloc[:, 1:].to_numpy(
            dtype=float))  # grid Q vs T vs Species
        self.QTref_284 = np.array(self.QT_interp_284(Tref))
        # identify index of QT grid (gQT) for each line
        self._QTmask = self.make_QTmask(self._ielem, self._iion)

        # Line strength: input shoud be ndarray not jnp array
        self.Sij0 = atomll.Sij0(self._A, self._gupper, self.nu_lines,
                                self._elower, self.QTref_284, self._QTmask, Irwin)  # 211013

        ### MASKING ###
        mask = (self.nu_lines > self.nurange[0]-self.margin)\
            * (self.nu_lines < self.nurange[1]+self.margin)\
            * (self.Sij0 > self.crit)

        self.masking(mask)
        if gpu_transfer:
            self.generate_jnp_arrays()
        
        # Compile atomic-specific data for each absorption line of interest
        ipccd = atomllapi.load_atomicdata()
        self.solarA = jnp.array(
            list(map(lambda x: ipccd[ipccd['ielem'] == x].iat[0, 4], self.ielem)))
        self.atomicmass = jnp.array(
            list(map(lambda x: ipccd[ipccd['ielem'] == x].iat[0, 5], self.ielem)))
        df_ionE = atomllapi.load_ionization_energies()
        self.ionE = jnp.array(
            list(map(atomllapi.pick_ionE, self.ielem, self.iion, [df_ionE, ] * len(self.ielem))))

    def masking(self, mask):
        """applying mask

        Args:
           mask: mask to be applied. self.mask is updated.

        """
        # numpy float 64 Do not convert them jnp array
        self.nu_lines = self.nu_lines[mask]
        self.Sij0 = self.Sij0[mask]
        self._A = self._A[mask]
        self._elower = self._elower[mask]
        self._eupper = self._eupper[mask]
        self._gupper = self._gupper[mask]
        self._jlower = self._jlower[mask]
        self._jupper = self._jupper[mask]
        self._QTmask = self._QTmask[mask]
        self._ielem = self._ielem[mask]
        self._iion = self._iion[mask]
        self._gamRad = self._gamRad[mask]
        self._gamSta = self._gamSta[mask]
        self._vdWdamp = self._vdWdamp[mask]

    def generate_jnp_arrays(self):
        """(re)generate jnp.arrays.

        Note:
           We have nd arrays and jnp arrays. We usually apply the mask to nd arrays and then generate jnp array from the corresponding nd array. For instance, self._A is nd array and self.A is jnp array.

        """
        # jnp arrays
        self.dev_nu_lines = jnp.array(self.nu_lines)
        self.logsij0 = jnp.array(np.log(self.Sij0))
        self.A = jnp.array(self._A)
        self.elower = jnp.array(self._elower)
        self.eupper = jnp.array(self._eupper)
        self.gupper = jnp.array(self._gupper)
        self.jlower = jnp.array(self._jlower, dtype=int)
        self.jupper = jnp.array(self._jupper, dtype=int)

        self.QTmask = jnp.array(self._QTmask, dtype=int)
        self.ielem = jnp.array(self._ielem, dtype=int)
        self.iion = jnp.array(self._iion, dtype=int)
        self.gamRad = jnp.array(self._gamRad)
        self.gamSta = jnp.array(self._gamSta)
        self.vdWdamp = jnp.array(self._vdWdamp)

    def Atomic_gQT(self, atomspecies):
        """Select grid of partition function especially for the species of
        interest.

        Args:
            atomspecies: species e.g., "Fe 1", "Sr 2", etc.

        Returns:
            gQT: grid Q(T) for the species
        """
        atomspecies_Roman = atomspecies.split(
            ' ')[0] + '_' + 'I'*int(atomspecies.split(' ')[-1])
        gQT = self.gQT_284species[np.where(
            self.pfdat['T[K]'] == atomspecies_Roman)][0]
        return gQT

    def QT_interp(self, atomspecies, T):
        """interpolated partition function The partition functions of Barklem &
        Collet (2016) are adopted.

        Args:
          atomspecies: species e.g., "Fe 1"
          T: temperature

        Returns:
          Q(T): interpolated in jnp.array for the Atomic Species
        """
        gQT = self.Atomic_gQT(atomspecies)
        QT = jnp.interp(T, self.T_gQT, gQT)
        return QT

    def QT_interp_Irwin_Fe(self, T, atomspecies='Fe 1'):
        """interpolated partition function This function is for the exceptional
        case where you want to adopt partition functions of Irwin (1981) for Fe
        I (Other species are not yet implemented).

        Args:
          atomspecies: species e.g., "Fe 1"
          T: temperature

        Returns:
          Q(T): interpolated in jnp.array for the Atomic Species
        """
        gQT = self.Atomic_gQT(atomspecies)
        QT = atomllapi.partfn_Fe(T)
        return QT

    def qr_interp(self, atomspecies, T):
        """interpolated partition function ratio The partition functions of
        Barklem & Collet (2016) are adopted.

        Args:
           T: temperature
           atomspecies: species e.g., "Fe 1"

        Returns:
           qr(T)=Q(T)/Q(Tref): interpolated in jnp.array
        """
        return self.QT_interp(atomspecies, T)/self.QT_interp(atomspecies, Tref)

    def qr_interp_Irwin_Fe(self, T, atomspecies='Fe 1'):
        """interpolated partition function ratio This function is for the
        exceptional case where you want to adopt partition functions of Irwin
        (1981) for Fe I (Other species are not yet implemented).

        Args:
           T: temperature
           atomspecies: species e.g., "Fe 1"

        Returns:
           qr(T)=Q(T)/Q(Tref): interpolated in jnp.array
        """
        return self.QT_interp_Irwin_Fe(T, atomspecies)/self.QT_interp_Irwin_Fe(Tref, atomspecies)

    def QT_interp_284(self, T):
        """interpolated partition function of all 284 species.

        Args:
           T: temperature

        Returns:
           Q(T)*284: interpolated in jnp.array for all 284 Atomic Species
        """
        list_gQT_eachspecies = self.gQT_284species.tolist()
        listofDA_gQT_eachspecies = list(
            map(lambda x: jnp.array(x), list_gQT_eachspecies))
        listofQT = list(map(lambda x: jnp.interp(
            T, self.T_gQT, x), listofDA_gQT_eachspecies))
        QT_284 = jnp.array(listofQT)
        return QT_284

    def make_QTmask(self, ielem, iion):
        """Convert the species identifier to the index for Q(Tref) grid (gQT)
        for each line.

        Args:
            ielem:  atomic number (e.g., Fe=26)
            iion:  ionized level (e.g., neutral=1, singly)

        Returns:
            QTmask_sp:  array of index of Q(Tref) grid (gQT) for each line
        """
        def species_to_QTmask(ielem, iion):
            sp_Roman = atomllapi.PeriodicTable[ielem] + '_' + 'I'*iion
            QTmask = np.where(self.pfdat['T[K]'] == sp_Roman)[0][0]
            return QTmask
        QTmask_sp = np.array(
            list(map(species_to_QTmask, ielem, iion))).astype('int')
        return QTmask_sp
