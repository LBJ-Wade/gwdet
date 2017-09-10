import sys
import os
import contextlib
import io
import urllib
import time
import warnings
import cPickle as pickle
import multiprocessing

import numpy as np
import astropy.cosmology
import scipy.stats
import scipy.interpolate
import pathos.multiprocessing

@contextlib.contextmanager
def nostdout():
    ''' Locally suppress stoud print. See https://stackoverflow.com/a/2829036'''
    save_stdout = sys.stdout
    sys.stdout = io.BytesIO()
    yield
    sys.stdout = save_stdout

with nostdout(): # pycbc prints a very annyoing new line when imported
    try:
        import pycbc.waveform
        import pycbc.filter
        import pycbc.psd
        has_pycbc=True
    except:
        warnings.warn("Can't import pycbc",ImportWarning)
        has_pycbc=False

directory = os.path.splitext(__file__)[0]+'_data'


def plotting():
    ''' Import stuff for plotting'''
    from matplotlib import use #Useful when working on SSH
    use('Agg')
    from matplotlib import rc #Set plot defaults
    font = {'family':'serif','serif':['cmr10'],'weight' : 'medium','size' : 16}
    rc('font', **font)
    rc('text',usetex=True)
    rc('figure',max_open_warning=1000)
    #matplotlib.rcParams['xtick.top'] = True
    #matplotlib.rcParams['ytick.right'] = True
    global plt
    import matplotlib.pyplot as plt


class pdet(object):

    def __init__(self,  directory=directory,
                        binfile=None,
                        mcn=int(1e8),
                        mcbins=int(1e5)):

        self._interpolate = None
        self.mcn = mcn # Size of monte carlo sample
        assert isinstance(self.mcn,(int,long))
        self.mcbins = mcbins # Number of bins before interpolating
        assert isinstance(self.mcbins,(int,long))

        self.directory=directory
        if binfile is None:
            binfile = 'pdet_'+'_'.join([x+'_'+str(eval(x)) for x in ['mcn','mcbins']]) +'.pkl'
        self.binfile=directory+'/'+binfile


    def montecarlo_samples(self,mcn):

        # Sample the angles according to their pdf
        theta=np.arccos(np.random.uniform(-1,1,mcn))   # Polar
        phi = np.pi*np.random.uniform(-1,1,mcn)        # Azimuthal
        psi = np.pi*np.random.uniform(-1,1,mcn)        # Azimuthal
        iota = np.arccos(np.random.uniform(-1,1,mcn))  # Polar

        # Antenna patterns. Eq. (57) in arxiv:0903.0338
        Fplus  = 0.5*(1.+np.cos(theta)**2)*np.cos(2.*phi)*np.cos(2.*psi) - np.cos(theta)*np.sin(2.*phi)*np.sin(2.*psi)
        Fcross = 0.5*(1.+np.cos(theta)**2)*np.cos(2.*phi)*np.sin(2.*psi) + np.cos(theta)*np.sin(2.*phi)*np.cos(2.*psi)

        # Projection parameter. Eq. (3.31) in arxiv:gr-qc/9301003 but define omega=Theta/4
        littleomega = ( Fplus**2*(1.+np.cos(iota)**2)**2/4. +Fcross**2*np.cos(iota)**2 )**0.5

        return littleomega if len(littleomega)>1 else littleomega[0]

    def interpolate(self):

        if self._interpolate is None:

            # Takes some time. Store a pickle...
            if not os.path.isfile(self.binfile):
                if not os.path.exists(self.directory):
                    os.makedirs(self.directory)
                print('['+self.__class__.__name__+'] Storing: '+self.binfile)

                print('['+self.__class__.__name__+'] Interpolating Pw(w)...')
                hist = np.histogram(self.montecarlo_samples(self.mcn),bins=self.mcbins)
                hist_dist = scipy.stats.rv_histogram(hist)

                with open(self.binfile, 'wb') as f: pickle.dump(hist_dist, f) #
            with open(self.binfile, 'rb') as f: hist_dist = pickle.load(f)

            self._interpolate = hist_dist.sf # sf give the cdf P(>w) instead of P(<w)

        return self._interpolate

    def eval(self,w):
        interpolant = self.interpolate()
        interpolated_values = interpolant(w)

        return interpolated_values# if len(interpolated_values)>1 else interpolated_values[0]

    def __call__(self,w):
        return self.eval(w)


# Workaround to make a class method pickable. Compatbile with singletons
# https://stackoverflow.com/a/40749513
def snr_pickable(x): return detectability()._snr(x)
def compute_pickable(x): return detectability()._compute(x)

class detectability(object):

    def __init__(self,  approximant='IMRPhenomD',
                        psd='aLIGOZeroDetHighPower',
                        flow=10.,
                        deltaf=1./40.,
                        snrthreshold=8.,
                        screen=False,
                        parallel=True,
                        massmin=1.,
                        massmax=100.,
                        zmin=1e-4,
                        zmax=2.2,
                        directory=directory,
                        binfile=None,
                        binfilepdet=None,
                        mc1d=int(200),
                        mcn=int(1e8),
                        mcbins=int(1e5),
                        has_pycbc=has_pycbc
                        ):

        self.approximant=approximant
        self.psd=psd
        self.flow=flow
        self.deltaf=deltaf
        self.snrthreshold=snrthreshold
        self.massmin=massmin
        self.massmax=massmax
        self.zmin=zmin
        self.zmax=zmax
        self.mc1d=mc1d # Size of grid where interpolation is performed
        assert isinstance(self.mc1d,(int,long))

        self.directory=directory
        if binfile is None:
            binfile = 'Pw_'+'_'.join([x+'_'+str(eval(x)) for x in ['approximant','psd','flow','deltaf','snrthreshold','massmin','massmax','zmin','zmax','mc1d']]) +'.pkl'
        self.binfile=directory+'/'+binfile
        #self.tempfile=directory+'/temp.pkl'

        # Flags
        self.screen=screen
        self.parallel=parallel
        if self.parallel and not os.path.isfile(self.binfile): # See https://stackoverflow.com/a/29030149/4481987
            self.map = pathos.multiprocessing.ProcessingPool(multiprocessing.cpu_count()).imap
        self.has_pycbc=has_pycbc

        # Parameters for pdet
        self.mcn = mcn
        self.mcbins = mcbins
        self.binfilepdet=binfilepdet

        # Lazy loading
        self._interpolate = None
        self._snrinterpolant = None
        self._pdetproj = None

    def pdetproj(self):
        if self._pdetproj is None:
            self._pdetproj = pdet(directory=directory,binfile=self.binfilepdet,mcn=self.mcn,mcbins=self.mcbins)
        return self._pdetproj

    def snr(self,m1_vals,m2_vals,z_vals):
        ''' Compute the SNR from m1,m2,z '''

        if not hasattr(m1_vals, "__len__"): m1_vals=[m1_vals]
        if not hasattr(m2_vals, "__len__"): m2_vals=[m2_vals]
        if not hasattr(z_vals, "__len__"): z_vals=[z_vals]

        snr=[]
        for m1,m2,z in zip(m1_vals,m2_vals,z_vals):
            lum_dist = astropy.cosmology.Planck15.luminosity_distance(z).value # luminosity distance in Mpc

            assert self.has_pycbc, "pycbc is needed"
            hp, hc = pycbc.waveform.get_fd_waveform(approximant=self.approximant,
                                                    mass1=m1*(1.+z),
                                                    mass2=m2*(1.+z),
                                                    delta_f=self.deltaf,
                                                    f_lower=self.flow,
                                                    distance=lum_dist)
            evaluatedpsd = pycbc.psd.analytical.from_string(self.psd,len(hp), self.deltaf, self.flow)
            snr_one=pycbc.filter.sigma(hp, psd=evaluatedpsd, low_frequency_cutoff=self.flow)
            snr.append(snr_one ) # use hp only because I want optimally oriented sources
            if self.screen==True:
               print("  m1="+str(m1)+" m1="+str(m2)+" z="+str(z)+" SNR="+str(snr_one))

        return np.array(snr) if len(snr)>1 else snr[0]

    def _snr(self,redshiftedmasses):
        ''' Utility function '''

        m1z,m2z = redshiftedmasses

        hp, hc = pycbc.waveform.get_fd_waveform(approximant=self.approximant,
                                                mass1=m1z,
                                                mass2=m2z,
                                                delta_f=self.deltaf,
                                                f_lower=self.flow,
                                                distance=1.)
        evaluatedpsd = pycbc.psd.analytical.from_string(self.psd,len(hp), self.deltaf, self.flow)
        snr=pycbc.filter.sigma(hp, psd=evaluatedpsd, low_frequency_cutoff=self.flow)
        if self.screen==True:
           print("  m1="+str(m1z)+" m1="+str(m2z)+" SNR="+str(snr))

        return snr

    def compute(cls,m1,m2,z):
        ''' Direct evaluation of the detection probability'''

        istance=cls()
        snr = istance.snr(m1,m2,z)
        return self.pdetproj.eval(istance.snrthreshold/snr)

    def _compute(self,data):
        ''' Utility function '''

        m1,m2,z=data
        ld = astropy.cosmology.Planck15.luminosity_distance(z).value
        snrint = self.snrinterpolant()
        snr = snrint([m1*(1+z),m2*(1+z)])/ld
        Pw= self.pdetproj().eval(self.snrthreshold/snr)

        return Pw

    def snrinterpolant(self):
        ''' Build an interpolation for the SNR '''


        if self._snrinterpolant is None:

            assert self.has_pycbc, 'pycbc is needed'

            # Takes some time. Store a pickle...

            # Takes some time. Store a pickle...
            #if not os.path.isfile(self.tempfile):


            print('['+self.__class__.__name__+'] Interpolating SNR...')

            # See https://stackoverflow.com/a/30059599

            m1z_grid = np.linspace(self.massmin*(1.+self.zmin),self.massmax*(1.+self.zmax),self.mc1d) # Redshifted mass 1
            m2z_grid = np.linspace(self.massmin*(1.+self.zmin),self.massmax*(1.+self.zmax),self.mc1d) # Redshifted mass 1
            grids=[m1z_grid,m2z_grid]

            #meshgrid=np.zeros(reduce(lambda x,y:x*y, [len(x) for x in grids]))

            #print(reduce(lambda x,y:x*y, [len(x) for x in grids]))
            meshgrid=[]
            meshcoord=[]
            for i,m1z in enumerate(m1z_grid):
                for j,m2z in enumerate(m2z_grid):
                        meshcoord.append([i,j])
                        meshgrid.append([m1z,m2z])
            meshgrid=np.array(meshgrid)
            meshcoord=np.array(meshcoord)


            if self.parallel:


                # Shuffle the arrays: https://stackoverflow.com/a/4602224/4481987
                # Useful to better ditribute load across processors
                if True:
                    assert len(meshcoord)==len(meshgrid)
                    p = np.random.permutation(len(meshcoord))
                    meshcoord = meshcoord[p]
                    meshgrid = meshgrid[p]

                #pool = multiprocessing.Pool(processes=multiprocessing.cpu_count())
                #meshvalues = pool.imap(snr_pickable, meshgrid)
                #pool.close() # No more work

                #meshvalues = pool.imap(self._snr, meshgrid)

                meshvalues= self.map(self._snr, meshgrid)
                while (True):
                    completed = meshvalues._index
                    if (completed == len(meshgrid)): break
                    print "   [multiprocessing] Waiting for", len(meshgrid)-completed, "tasks..."
                    time.sleep(1)

                #pool.close()
                #pool.join()

            else:
                meshvalues = map(self._snr, meshgrid)

            #print meshvalues

            valuesforinterpolator = np.zeros([len(x) for x in grids])
            for ij,val in zip(meshcoord,meshvalues):
                i,j=ij
                valuesforinterpolator[i,j]=val

            snrinterpolant = scipy.interpolate.RegularGridInterpolator(points=grids,values=valuesforinterpolator,bounds_error=False,fill_value=None)

            self._snrinterpolant = snrinterpolant

            #    with open(self.tempfile, 'wb') as f: pickle.dump(snrinterpolant, f)

            #with open(self.tempfile, 'rb') as f: self._snrinterpolant = pickle.load(f)


        return self._snrinterpolant


    def interpolate(self):
        ''' Build an interpolation for the detection probability as a function of m1,m2,z'''

        if self._interpolate is None:
            assert self.has_pycbc, "pycbc is needed"

            # Takes some time. Store a pickle...
            if not os.path.isfile(self.binfile):
                if not os.path.exists(self.directory):
                    os.makedirs(self.directory)


                print('['+self.__class__.__name__+'] Storing: '+self.binfile)

                # Make sure the SNR interpolant is available
                dummy=self.snrinterpolant()
                # Make sure the P(w) interpolant is available
                dummy = self.pdetproj()(0.5)

                print('['+self.__class__.__name__+'] Interpolating Pw(SNR)...')

                # See https://stackoverflow.com/a/30059599
                m1_grid = np.linspace(self.massmin,self.massmax,self.mc1d) # Redshifted mass 1
                m2_grid = np.linspace(self.massmin,self.massmax,self.mc1d) # Redshifted mass 1
                z_grid = np.linspace(self.zmin,self.zmax,self.mc1d) # Redshifted mass 1

                grids=[m1_grid,m2_grid,z_grid]

                meshgrid=[]
                meshcoord=[]
                for i,m1 in enumerate(m1_grid):
                    for j,m2 in enumerate(m2_grid):
                            for k,z in enumerate(z_grid):
                                #print i,j,k
                                meshcoord.append([i,j,k])
                                meshgrid.append([m1,m2,z])
                meshgrid=np.array(meshgrid)
                meshcoord=np.array(meshcoord)

                if self.parallel:

                    # Shuffle the arrays: https://stackoverflow.com/a/4602224/4481987
                    # Useful to better ditribute load across processors
                    if True:
                        assert len(meshcoord)==len(meshgrid)
                        p = np.random.permutation(len(meshcoord))
                        meshcoord = meshcoord[p]
                        meshgrid = meshgrid[p]

                    #pool = multiprocessing.Pool(processes=multiprocessing.cpu_count())
                    #meshvalues = pool.imap(compute_pickable, meshgrid)
                    #pool.close() # No more work

                    #pool2 = pathos.multiprocessing.ProcessingPool(multiprocessing.cpu_count())
                    #meshvalues = pool2.imap(self._compute, meshgrid)
                    #pool2.close()

                    meshvalues= self.map(self._compute, meshgrid)



                    while (True):
                        completed = meshvalues._index
                        if (completed == len(meshgrid)): break
                        print "   [multiprocessing] Waiting for", len(meshgrid)-completed, "tasks..."
                        time.sleep(1)
                    #pool.close()

                else:
                    meshvalues = map(self._compute, meshgrid)

                print meshvalues

                valuesforinterpolator = np.zeros([len(x) for x in grids])
                for ijk,val in zip(meshcoord,meshvalues):
                    i,j,k=ijk
                    valuesforinterpolator[i,j,k]=val

                interpolant = scipy.interpolate.RegularGridInterpolator(points=grids,values=valuesforinterpolator,bounds_error=False,fill_value=None)

                with open(self.binfile, 'wb') as f: pickle.dump(interpolant, f)

            with open(self.binfile, 'rb') as f: interpolant = pickle.load(f)

            self._interpolate = interpolant

        return self._interpolate


    def eval(self,m1,m2,z):
        ''' Evaluate the detection probability from the interpolation'''

        if not hasattr(m1, "__len__"): m1=[m1]
        if not hasattr(m2, "__len__"): m2=[m2]
        if not hasattr(z, "__len__"): z=[z]

        interpolant = self.interpolate()

        interpolated_values = interpolant(np.transpose([m1,m2,z]))

        return interpolated_values if len(interpolated_values)>1 else interpolated_values[0]

    def __call__(self,m1,m2,z):
        return self.eval(m1,m2,z)


#dp=detprob(screen=False,parallel=True)
#print dp.compute(10,10,0.1)



#ciao=detprob.compute(10,10,0.1)


def compare_Pw():
    ''' Compare performance of the pdet interpolator against public data from Emanuele Berti's website'''

    plotting()# Initialized plotting stuff

    # Download file from Emanuele Berti's website if it does not exist
    if not os.path.isfile("Pw_single.dat"):
        urllib.urlretrieve('http://www.phy.olemiss.edu/~berti/research/Pw_single.dat', "Pw_single.dat")

    wEm,PwEm=np.loadtxt("Pw_single.dat",unpack=True)
    wmy = np.linspace(-0.1,1.1,1000)
    p=pdet()
    Pwmy=p(wmy)
    f, ax = plt.subplots(2, sharex=True)

    ax[0].plot(wEm,PwEm,label="public data")
    ax[0].plot(wmy,Pwmy,ls='dashed',label="this code")
    Pwmy = p(wEm)
    ax[1].plot(wEm,np.abs(PwEm-Pwmy),c='C2')#*2./(PwEm+Pwmy))
    ax[1].set_xlabel('$\omega$')
    ax[0].set_ylabel('$P(\omega)$')
    ax[1].set_ylabel('Residuals')
    ax[0].legend()
    plt.savefig(sys._getframe().f_code.co_name+".pdf",bbox_inches='tight')


def compare_Psnr():

    plotting() # Initialized plotting stuff

    #dp=detprob(screen=False,parallel=True)

    computed=[]
    interpolated=[]

    n=10000


    m1=np.random.uniform(1,100,n)
    m2=np.random.uniform(1,100,n)
    z=np.random.uniform(1e-4,2.5,n)

    computed=detprob.compute(m1,m2,z)
    interpolated=detprobk.eval(m1,m2,z)

    computed=np.array(computed)
    interpolated=np.array(interpolated)
    residuals=np.abs(computed-interpolated)

    residuals_notzero= residuals[np.logical_and(computed!=0,interpolated!=0)]

    f, ax = plt.subplots(2)

    #ax[0].hist(residuals,weights=[1./n for x in residuals],alpha=0.8,bins=100)
    ax[0].hist(residuals_notzero,weights=[1./n for x in residuals_notzero],alpha=0.8,bins=100)
    ax[0].axvline(np.median(residuals_notzero),ls='dashed',c='red')
    #ax[1].hist(residuals,range=[0,0.0001],weights=[1./n for x in residuals],alpha=0.8,bins=100)
    ax[1].hist(residuals_notzero,range=[0,0.0001],weights=[1./n for x in residuals_notzero],alpha=0.8,bins=100)
    ax[1].axvline(np.median(residuals_notzero),ls='dashed',c='red')
    ax[1].set_xlabel('Residuals $P_{\\rm det}$')

    plt.savefig(sys._getframe().f_code.co_name+".pdf",bbox_inches='tight')


#p= pdet()
#print(p(0.5))
#
#p = detectability(parallel=True)
#print(p(10,10,0.1))

compare_Pw()

#compare_Psnr()

    #
    #
    # def _compute(self,data):
    #     m1,m2,z=data
    #     snrint = self.snrinterpolant()
    #
    #     snr=snrint([m1*(1.*z),m2*(1.+z)])/astropy.cosmology.Planck15.luminosity_distance(z).value
    #     print "SNRS", snrint([m1*(1.*z),m1*(1.*z)]), self._snr([m1*(1.*z),m1*(1.*z)]), snr, self.snr(m1,m2,z)
    #
    #     Pw= Pomega.eval(self.snr_threshold/snr)
    #
    #     return Pw


#


#
#
#
# def compute_my_detprob():
#
#
#
#
# def Pwfloor(w,interp):
#     '''Return 0 if SNR is below treshold.'''
#     if w>=1. :
#         return 0.
#     elif w<=0. :
#         return 1.
#     else:
#         return float(interp(w))
#
#
# def getSNR(m1,m2,z):
#     ''' Compute the SNR using pycbc'''
#
#     lum_dist = cosmo.luminosity_distance(z).value # luminosity distance in Mpc
#     print(z,lum_dist)
#     # get waveform
#     flow=10
#     hp, hc = pycbc.waveform.get_fd_waveform(approximant='IMRPhenomD',mass1=m1*(1.+z),mass2=m2*(1.+z),delta_f=1./40.,f_lower=flow,distance=lum_dist)
#     psd = pycbc.psd.aLIGOZeroDetHighPower(len(hp), hp.delta_f, flow)
#     snr = pycbc.filter.sigma(hp, psd=psd, low_frequency_cutoff=flow) # use hp only because I want optimally oriented sources
#
#     return snr
#
# def compute_detected_probability(snr,SNRthr=8):
#
#     pfint= Pwint() # Interpolate the peanut factor function  for all
#     #detprob= np.array([Pwfloor(SNRthr/x,pfint) for x in snr]) # Compute dection probabilities
#     detprob= Pwfloor(SNRthr/snr,pfint)# Compute dection probabilities
#
#     return detprob
