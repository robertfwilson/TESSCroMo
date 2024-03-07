import numpy as np
import matplotlib.pyplot as plt
import matplotlib as mpl
from astropy.io import fits
from glob import glob


from matplotlib import patches

import PRF


from astropy.coordinates import SkyCoord, Angle
from astropy.wcs import WCS
from astroquery.mast import Catalogs
import astropy.units as u
from astropy.time import Time

from .plotcromo import *
#from .utils import *





def get_tic_sources(ticid, tpf_shape=[14,14]):

    pix_scale = 21.0

    try:
        catalogTIC = Catalogs.query_object('TIC '+str(ticid), radius=Angle(np.max(tpf_shape) * pix_scale, "arcsec"), catalog="TIC")
    except:
        catalogTIC = Catalogs.query_object(str(ticid), radius=Angle(np.max(tpf_shape) * pix_scale, "arcsec"), catalog="TIC")

    return catalogTIC





def estimate_offset_gadient(tpfmodel, tpf, err=None):

    g0,g1 = np.gradient(tpfmodel, edge_order=1)

    data = np.vstack((tpf-tpfmodel).reshape(-1, ))
    A = np.vstack( [g0.reshape(-1), g1.reshape(-1)] ).T

    if not(err is None):
        A = np.vstack(1./err.reshape(-1)**2.)*A
        data = np.vstack(1./err.reshape(-1)**2.)*data
            
    w = np.linalg.solve( A.T.dot(A) , A.T.dot(data) )

    return w



class TESSTargetPixelModeler(object):

    def __init__(self, TPF):

        self.tpf = TPF

        self.tic_id = self.tpf.targetid
        
        nan_mask = ~np.isnan(np.sum(np.sum(self.tpf.flux.value, axis=1), axis=1) )
        self.nan_mask = nan_mask
        self.time = TPF.time[nan_mask]
        self.cadenceno = TPF.cadenceno[nan_mask]


        self.tpf_wcs = self.tpf.wcs
        self.tpf_med_data = np.median(self.tpf.flux.value[nan_mask], axis=0)
        self.tpf_med_err = np.sum(self.tpf.flux_err.value[nan_mask]**2., axis=0)/len(self.tpf.flux_err[nan_mask]) 

        self.tpf_flux = self.tpf.flux.value[nan_mask]
        self.tpf_flux_err = self.tpf.flux_err.value[nan_mask]
        
        
        self.prf = self._get_prfmodel()
        self.catalog = self._get_tic_sources()

        self.row_ref = 0#self.tpf.hdu[1].header['1CRPX4']-self.tpf_med_data.shape[0]//2
        self.col_ref = 0#self.tpf.hdu[1].header['1CRPX4']-self.tpf_med_data.shape[1]//2


    def from_tesscut(self, TPF):

        return 1. 
        
    def _get_prfmodel(self, prf_dir=None):

        return PRF.TESS_PRF(self.tpf.camera,self.tpf.ccd,self.tpf.sector,self.tpf.column,self.tpf.row)

    def _get_tic_sources(self, directory=None, mag_lim=20.):

        
        if directory is None:
            try:
                catalogTIC = get_tic_sources(self.tic_id, tpf_shape=self.tpf_med_data.shape)
            except:
                catalogTIC = get_tic_sources(self.tic_id, tpf_shape=self.tpf_med_data.shape)
        
        mag_cut = catalogTIC['Tmag'] < mag_lim
        source_catalog = catalogTIC.to_pandas().loc[mag_cut]

        self.catalog=source_catalog

        return source_catalog 


    def generate_bkg_source_model(self, flux_scale=None, **kwargs):

        star_row_col = self._get_source_row_col()
        star_mags = self.catalog['Tmag'].to_numpy()

        bkg_source_tpfmodel = self._generate_tpf_scene(star_row_col[1:], star_mags[1:], **kwargs)
        
        return bkg_source_tpfmodel*self.bestfit_flux_scale 

    def generate_source_model(self, flux_scale=None, **kwargs):

        star_row_col = self._get_source_row_col()
        star_mags = self.catalog['Tmag'].to_numpy()

        source_tpfmodel = self._generate_tpf_scene(star_row_col[:1], star_mags[:1], **kwargs)

        return source_tpfmodel*self.bestfit_flux_scale 

    
    def _get_star_scene(self, **kwargs):

        star_row_col = self._get_source_row_col()
        star_mags = self.catalog['Tmag'].to_numpy()

        all_star_scene = self._generate_tpf_scene(star_row_col, star_mags, **kwargs)

        return all_star_scene

    
    def _get_source_row_col(self, ):

        
        # Propagate star positions by proper motions
        refepoch = 2015.5
        referenceyear = Time(refepoch, format='decimalyear', scale='utc')
        deltayear = (self.time[0] - referenceyear).to(u.year)
        pmra = ((np.nan_to_num(np.asarray(self.catalog.pmRA)) * u.milliarcsecond/u.year) * deltayear).to(u.deg).value
        pmdec = ((np.nan_to_num(np.asarray(self.catalog.pmDEC)) * u.milliarcsecond/u.year) * deltayear).to(u.deg).value
        #self.catalog.RA_orig += pmra
        #self.catalog.Dec_orig += pmdec
        radecs = np.vstack([self.catalog['RA_orig']+pmra, self.catalog['Dec_orig']+pmdec]).T

        # check for nans in RA_orig/Dec_orig (usually Gaia DR2), replace with generic RA, Dec from TIC 
        bad_radec = np.isnan(radecs[:,0])
        radecs[bad_radec,:] = np.vstack([self.catalog['ra'], self.catalog['dec']]).T[bad_radec,:]
        
        coords = self.tpf_wcs.all_world2pix(radecs, 0)
            
        return coords



    def _generate_tpf_scene(self, source_xy, source_mags, dx=0, dy=0, buffer=5):

        #scene = np.zeros_like(self.tpf)

        size_x, size_y = self.tpf_med_data.shape
        buffer_size = (size_x+2*buffer, size_y+2*buffer)
        
        scene = np.zeros(buffer_size)

        dx+=buffer
        dy+=buffer

        for i in range(len(source_xy)):

            star_row, star_col = source_xy[i]
            star_mag = source_mags[i]

            try:
                scene += self.prf.locate(star_row-(self.row_ref-dx), star_col-(self.col_ref-dy), buffer_size) * 10.**(-0.4*(star_mag-20.44))
            except ValueError:
                pass

        return scene[buffer:-buffer,buffer:-buffer]


    def estimate_offset(self, fit_tpf=True, use_err=True):

        if fit_tpf or (self.bestfit_tpfmodel is None):
            tpfmodel, _, _ = self.fit_tpf_model(use_err=use_err, )
        
        else:
            tpfmodel =  self.bestfit_tpfmodel

        weights = estimate_offset_gadient(tpfmodel, self.tpf_med_data, self.tpf_med_err)

        self.bestfit_dx = weights.T[0][0]
        self.bestfit_dy = weights.T[0][1]
        

        return weights.T[0] 


    def estimate_offset_coarse(self, dx_range=[-0.5, 0.5], dy_range=[-0.5, 0.5], step=0.1, **kwargs):

        dys = np.arange(dy_range[0], dy_range[1], step)
        dxs = np.arange(dx_range[0], dx_range[1], step)

        offsets = np.stack(np.meshgrid(dxs, dys)).T.reshape(-1, 2)

        # Set up Data
        err = np.vstack(self.tpf_err.reshape(-1, ) )
        data = np.vstack(self.tpf_med_data.reshape(-1, ) )
        data_err = data*np.vstack(1./err.reshape(-1)**2.)

        chi2_values = []
        
        for dx,dy in offsets:
            # Linear Algebra Least Squares Fitting

            star_tpf_model = self._get_star_scene(dx=dx, dy=dy, **kwargs)
            
            A = np.vstack([star_tpf_model.reshape(-1), np.ones_like(star_tpf_model).reshape(-1)]).T
            A_err = A*np.vstack(1./err.reshape(-1)**2.)
            
            w = np.linalg.solve( A.T.dot(A) , A.T.dot(data) )

            chi2_values.append( np.sum((A.dot(w) - data)/err**2.) )


        best_dx, best_dy = offsets[np.argmin(chi2_values)] 
        
        return best_dx, best_dy


    def fit_tpf_model(self, use_err=True, **kwargs):

        
        star_tpf_model = self._get_star_scene(**kwargs)

        # Linear Algebra Least Squares Fitting
        data = np.vstack(self.tpf_med_data.reshape(-1, ) )        
        A = np.vstack([star_tpf_model.reshape(-1), np.ones_like(star_tpf_model).reshape(-1)]).T

        if use_err:
            err = self.tpf_med_err
            A = np.vstack(1./err.reshape(-1)**2.)*A
            data = np.vstack(1./err.reshape(-1)**2.)*data
            
        w = np.linalg.solve( A.T.dot(A) , A.T.dot(data) )
        
        flux_scale_factor, bkg_flux = w.T[0]

        fit_tpf_model = star_tpf_model*flux_scale_factor+bkg_flux

        self.bestfit_tpfmodel = fit_tpf_model
        self.bestfit_flux_scale = flux_scale_factor
        self.bestfit_bkg_flux = bkg_flux

        return fit_tpf_model, flux_scale_factor, bkg_flux



    def get_contamination_ratio(self, fit_tpf=True, use_err=True, aperture=None, **kwargs):

        if aperture is None:
            aperture = self._get_aperture()

        if fit_tpf:
            self.fit_tpf_model(use_err=use_err, **kwargs)
    
        source_tpf = self.generate_source_model(**kwargs)
        contam_tpf = self.generate_bkg_source_model(**kwargs)

        bkg_ap_flux = self.bestfit_bkg_flux * np.sum(aperture)
        total_sum_flux = np.sum( aperture * (self.bestfit_tpfmodel-self.bestfit_bkg_flux) )
        source_sum_flux = np.sum(aperture * source_tpf )
        contam_sum_flux =np.sum(  aperture * contam_tpf )
        
        return {'crowdsap': contam_sum_flux/total_sum_flux,
                'flfrcsap': source_sum_flux/np.sum(source_tpf),
                'dilution': total_sum_flux/source_sum_flux, 
                'med_tpf_bkg_aperture_flux':bkg_ap_flux, 
                'tess_zeropoint_mag': 20.44-2.5*np.log10(self.bestfit_flux_scale) }   



    def plot_tpf_model(self, plot_color='C1', logscale=True, vmin=None, vmax=None):

        star_rowcol = self._get_source_row_col()
        star_mags = self.catalog['Tmag'].to_numpy()


        fig, (ax1,ax2, ax3) = plt.subplots(1,3, figsize=(8,4) , constrained_layout=True,sharex=True, sharey=True)


        if vmin is None:
            vmin = np.min(np.abs(self.bestfit_tpfmodel) )
        if vmax is None:
            vmax = np.max(self.bestfit_tpfmodel)

        if logscale:
        
            cax1=ax1.imshow(self.tpf_med_data, origin='lower', norm=mpl.colors.LogNorm(vmin=vmin,vmax=vmax), )
            cax2=ax2.imshow(self.bestfit_tpfmodel, origin='lower', norm=mpl.colors.LogNorm(vmin=vmin,vmax=vmax), )
        else:
            cax1=ax1.imshow(self.tpf_med_data, origin='lower', vmin=vmin,vmax=vmax, )
            cax2=ax2.imshow(self.bestfit_tpfmodel, origin='lower', vmin=vmin, vmax=vmax )

        cax3=ax3.imshow((self.bestfit_tpfmodel-self.tpf_med_data), origin='lower', cmap='coolwarm', )
            
        
        plt.colorbar(ax=[ax1,ax2], mappable=cax1, location='bottom', label='Flux [e-/sec]', shrink=0.5 )
        #plt.colorbar(ax=ax2, mappable=cax2, location='bottom', label='Flux [e-/sec]')
        plt.colorbar(ax=ax3, mappable=cax3, location='bottom', label='Residual [e-/sec]')
        
        
        for ax in (ax1,ax2,ax3):
            ax.set_xticks([])
            ax.set_yticks([])

            ax.scatter(star_rowcol.T[0]-self.row_ref-self.bestfit_dx, star_rowcol.T[1]-self.col_ref-self.bestfit_dy, s=(star_mags-20.44)**2., marker='*', 
                       edgecolor=plot_color, color='w' , zorder=5)
            plot_aperture(ax=ax, aperture_mask=self.get_optimal_aperture()[0], mask_color=plot_color)
            plot_ne_arrow(ax=ax, x_0=self.tpf_med_data.shape[0]*0.15, y_0=self.tpf_med_data.shape[0]*0.8, 
                          len_pix=self.tpf_med_data.shape[0]*0.1, wcs=self.tpf_wcs)
        
        ax1.set_title('Median TPF')
        ax2.set_title('Model TPF')
        ax3.set_title('Model$-$Data')
        ax1.set_xlim(-0.5, self.tpf_med_data.shape[0]-0.5)
        ax1.set_ylim(-0.5, self.tpf_med_data.shape[1]-0.5)

        #plt.suptitle('{}: Sector {}'.format(self.target_id, self.sector))

        return ax1,ax2,ax3


    def get_optimal_aperture(self, snr_limit=1., **kwargs):

        source_tpf = self.generate_source_model(**kwargs)
        contam_tpf = self.generate_bkg_source_model(**kwargs)
        bkg_tpf = np.zeros_like(contam_tpf)+self.bestfit_bkg_flux

        tpf_source_snr = source_tpf / np.sqrt(source_tpf+contam_tpf+bkg_tpf)
        best_aperture = tpf_source_snr>snr_limit

        self.best_aperture = best_aperture

        bkg_aperture = ((source_tpf+contam_tpf) / np.sqrt(source_tpf+contam_tpf+bkg_tpf)) < 1.
        
        return best_aperture, bkg_aperture




    def get_prf_xy_timeseries(self, use_err=True, **kwargs):

        if self.bestfit_tpfmodel is None:
            basemodel, _, _ = self.fit_tpf_model(use_err=use_err, )
        else:
            basemodel =  self.bestfit_tpfmodel

        tpf_fluxes = self.tpf_flux
        tpf_flux_errs = self.tpf_flux_err

        ws = [estimate_offset_gadient(basemodel, tpf_fluxes[i], tpf_flux_errs[i]).T[0] for i in range(tpf_fluxes.shape[0])]

        #print(ws)
        self.prf_dx, self.prf_dy = np.array(ws).T
        
        return ws


    def get_crowding_timeseries(self, aperture=None, ):

        
        

        
        source_tpf = self.generate_source_model(**kwargs)
        contam_tpf = self.generate_bkg_source_model(**kwargs)

        
        

        '''
        under construction
        '''
        return 1.

    def get_sap_flux_timeseries(self,aperture=None, **kwargs):

        if aperture is None:
            best_aperture = self.get_optimal_aperture(**kwargs)
        else:
            best_aperture=aperture

        try:
            dx_t, dy_t = self.prf_dx, self.prf_dy
        except:
            dxdt =  self.get_prf_xy_timeseries(use_err=True, **kwargs)
            dx_t, dy_t = np.array(dxdt).T



        sapflux_timeseries = np.array([])
        flfrc_sapflux_timeseries = np.array([])
        contam_sapflux_timeseries = np.array([])
        bkg_sapflux_timeseries = np.array([])
       
        
        #flux_scale_factor, bkg_flux = w.T[0]
        #fit_tpf_model = star_tpf_model*flux_scale_factor+bkg_flux

        
        tpf_fluxes = self.tpf_flux
        tpf_flux_errs = self.tpf_flux_err
        
        #(sap_flux - np.median(sap_flux)*crowding['crowdsap']) / crowding['flfrcsap']

        bkg_flux_array = np.ones_like(tpf_fluxes[0]).ravel()

        for i in range(len(dx_t)):


            source_tpf = self.generate_source_model(dx=dx_t[i], dy=dy_t[i],)
            contam_tpf = self.generate_bkg_source_model(dx=dx_t[i], dy=dy_t[i],)
            allstar_tpf = source_tpf+contam_tpf

            contam_frac = np.sum(contam_tpf*best_aperture) / np.sum(allstar_tpf*best_aperture)
            flux_frac = np.sum(source_tpf*best_aperture) / np.sum(source_tpf)

            #print(sap_flux_i, contam_frac, flux_frac)

            # Fit for the Bkg Flux
            data = np.vstack(1./tpf_flux_errs[i].ravel()**2.)*np.vstack(tpf_fluxes[i].ravel())
            A = np.vstack(1./tpf_flux_errs[i].reshape(-1)**2.)*np.vstack([allstar_tpf.reshape(-1), bkg_flux_array]).T
            w = np.linalg.solve( A.T.dot(A) , A.T.dot(data) )

            #print(w)
            
            zero_flux_i, bkg_i = w.T[0]

            sap_flux_i = np.sum(best_aperture*tpf_fluxes[i])

            sap_flux_bkg_sub = sap_flux_i - np.sum(bkg_i*best_aperture)
            sap_flux_decrowd = sap_flux_bkg_sub * (1. - contam_frac)

            sap_flux_frac_corr = sap_flux_decrowd/flux_frac

            sapflux_timeseries = np.append(sapflux_timeseries, sap_flux_frac_corr)
            flfrc_sapflux_timeseries = np.append(flfrc_sapflux_timeseries, flux_frac)
            contam_sapflux_timeseries = np.array(contam_sapflux_timeseries, contam_frac)
        

        return sapflux_timeseries #flfrc_sapflux_timeseries, contam_sapflux_timeseries




    def frame_solve(self, frame, dx=0, dy=0, n_bkg_terms=1):

        source = self.generate_source_model(dx=dx,dy=dy)
        source/=np.sum(source)
        bkg_stars = self.generate_bkg_source_model(dx=dx,dy=dy)

        bkg = np.ones_like(frame)

        #bkg_x, bkg_y = np.meshgrid(frame.shape[0], frame.shape[1])

        #bkg_terms = [bkg_x**n, bkg_y**n]

        #[bkg_terms.append(bkg_x**n) for n in range(1, n_bkg_terms+1)]
        #[bkg_terms.append(bkg_y**n) for n in range(1, n_bkg_terms+1)]
            
        A = np.vstack([source.ravel(),bkg_stars.ravel(),          
                bkg.ravel()]).T

        return np.linalg.solve(A.T.dot(A), A.T.dot(frame.ravel()))


    def matrix_solve():
        return 1. 
        

    def get_prf_flux_timeseries(self,  progress=False, **kwargs):

        
        all_stars = self._get_star_scene(**kwargs)
        bkg = np.ones_like(all_stars)

        y=self.tpf_flux

        dxdt = self.get_prf_xy_timeseries()
        dx_t, dy_t = np.array(dxdt).T
        
    
        if progress:
            from tqdm import tqdm
            iterable = tqdm(np.arange(len(y) ).astype(int) )
        else:
            iterable = np.arange(len(y) ).astype(int)


        ws = np.asarray([self.frame_solve(y[i], dx_t[i], dy_t[i], n_bkg_terms=2) for i in iterable])
        
        #for i, frame in iterable:
            
        #    source = self.generate_source_model(dx=dx_t[i],dy=dy_t[i])
        #    bkg_stars = self.generate_bkg_source_model(dx=dx_t[i],dy=dy_t[i])
            
        #    A = np.vstack([source.ravel(),bkg_stars.ravel(),          
        #        bkg.ravel()]).T

        #    ws_i = np.linalg.solve(A.T.dot(A), A.T.dot(frame.ravel()))

        #    ws = np.append(ws, ws_i)
            

        #model = np.asarray([A[:, :-1].dot(w[:-1]).reshape(y.shape[1:]) for w in ws])self

        prf_flux, zero_point_flux, bkg_flux = ws.T

        return prf_flux, zero_point_flux, bkg_flux, dx_t, dy_t


