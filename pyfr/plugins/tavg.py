import re

import numpy as np

from pyfr.inifile import Inifile
from pyfr.mpiutil import get_comm_rank_root, mpi
from pyfr.nputil import npeval
from pyfr.plugins.base import BasePlugin, PostactionMixin, RegionMixin
from pyfr.writers.native import NativeWriter


class TavgPlugin(PostactionMixin, RegionMixin, BasePlugin):
    name = 'tavg'
    systems = ['*']
    formulations = ['dual', 'std']

    def __init__(self, intg, cfgsect, suffix=None):
        super().__init__(intg, cfgsect, suffix)

        comm, rank, root = get_comm_rank_root()

        # Underlying elements class
        self.elementscls = intg.system.elementscls

        # Averaging mode
        self.mode = self.cfg.get(cfgsect, 'mode', 'windowed')
        if self.mode not in {'continuous', 'windowed'}:
            raise ValueError('Invalid averaging mode')

        # Std deviation mode
        self.std_mode = self.cfg.get(cfgsect, 'std-mode', 'summary')
        if self.std_mode not in {'summary', 'all'}:
            raise ValueError('Invalid standard deviation mode')

        # Expressions pre-processing
        self._prepare_exprs()

        # Floating point precision
        self.delta_h = np.finfo(np.float64).eps**0.5

        # Output data type
        fpdtype = self.cfg.get(cfgsect, 'precision', 'single')
        if fpdtype == 'single':
            self.fpdtype = np.float32
        elif fpdtype == 'double':
            self.fpdtype = np.float64
        else:
            raise ValueError('Invalid floating point data type')

        # Base output directory and file name
        basedir = self.cfg.getpath(self.cfgsect, 'basedir', '.', abs=True)
        basename = self.cfg.get(self.cfgsect, 'basename')

        # Construct the file writer
        self._writer = NativeWriter(intg, basedir, basename, 'tavg')

        # Gradient pre-processing
        self._init_gradients()

        # Time averaging parameters
        self.tstart = self.cfg.getfloat(cfgsect, 'tstart', 0.0)
        self.dtout = self.cfg.getfloat(cfgsect, 'dt-out')
        self.nsteps = self.cfg.getint(cfgsect, 'nsteps')

        # Register our output times with the integrator
        intg.call_plugin_dt(self.dtout)

        # Mark ourselves as not currently averaging
        self._started = False

        # Get the total number of solution points in the region
        em = intg.system.ele_map
        ergn = self._ele_regions
        if self.cfg.get(self.cfgsect, 'region') == '*':
            tpts = sum(em[e].nupts * em[e].neles for _, e, _ in ergn)
        else:
            tpts = sum(len(r) * em[e].nupts for _, e, r in ergn)

        # Reduce
        self.tpts = comm.reduce(tpts, op=mpi.SUM, root=root)

    def _prepare_exprs(self):
        cfg, cfgsect = self.cfg, self.cfgsect
        c = self.cfg.items_as('constants', float)
        self.anames, self.aexprs = [], []
        self.outfields, self.fexprs = [], []
        self.vnames, self.fnames = [], []

        # Iterate over accumulation expressions first
        for k in cfg.items(cfgsect, prefix='avg-'):
            self.anames.append(k.removeprefix('avg-'))
            self.aexprs.append(cfg.getexpr(cfgsect, k, subs=c))
            self.outfields.append(k)

        # Followed by any functional expressions
        for k in cfg.items(cfgsect, prefix='fun-avg-'):
            self.fnames.append(k.removeprefix('fun-avg-'))
            self.fexprs.append(cfg.getexpr(cfgsect, k, subs=c))
            self.outfields.append(k)

        # Create fields for std deviations
        if self.std_mode == 'all':
            for k in cfg.items(cfgsect, prefix='avg-'):
                self.outfields.append(f'std-{k[4:]}')

            for k in cfg.items(cfgsect, prefix='fun-avg-'):
                self.outfields.append(f'fun-std-{k[4:]}')

    def _init_gradients(self):
        # Determine what gradients, if any, are required
        gradpnames = set()
        for ex in self.aexprs:
            gradpnames.update(re.findall(r'\bgrad_(.+?)_[xyz]\b', ex))

        privarmap = self.elementscls.privarmap[self.ndims]
        self._gradpinfo = [(pname, privarmap.index(pname))
                           for pname in gradpnames]

    def _init_accumex(self, intg):
        comm, rank, root = get_comm_rank_root()
        self.tstart_acc = self.prevt = self.tout_last = intg.tcurr
        self.prevex = self._eval_acc_exprs(intg)
        self.accex = [np.zeros_like(p, dtype=np.float64) for p in self.prevex]
        self.vaccex = [np.zeros_like(a) for a in self.accex]

    def _eval_acc_exprs(self, intg):
        exprs = []

        # Get the primitive variable names
        pnames = self.elementscls.privarmap[self.ndims]

        # Compute the gradients
        if self._gradpinfo:
            grad_soln = intg.grad_soln

        # Iterate over each element type in the simulation
        for idx, etype, rgn in self._ele_regions:
            soln = intg.soln[idx][..., rgn].swapaxes(0, 1)

            # Convert from conservative to primitive variables
            psolns = self.elementscls.con_to_pri(soln, self.cfg)

            # Prepare the substitutions dictionary
            subs = dict(zip(pnames, psolns))

            # Prepare any required gradients
            if self._gradpinfo:
                grads = np.rollaxis(grad_soln[idx], 2)[..., rgn]

                # Transform from conservative to primitive gradients
                pgrads = self.elementscls.grad_con_to_pri(soln, grads,
                                                          self.cfg)

                # Add them to the substitutions dictionary
                for pname, idx in self._gradpinfo:
                    for dim, grad in zip('xyz', pgrads[idx]):
                        subs[f'grad_{pname}_{dim}'] = grad

            # Evaluate the expressions
            exprs.append(np.array([npeval(v, subs) for v in self.aexprs]))

        return exprs

    def _eval_fun_exprs(self, avars):
        # Prepare the substitution dictionary
        subs = dict(zip(self.anames, avars))

        # Evaluate the function and return
        return np.array([npeval(v, subs) for v in self.fexprs])

    def _eval_fun_var(self, dev, accex):
        dfexpr, exprs = [], []

        # Iterate over each element type our averaging region
        for av in accex:
            df = []

            # Evaluate the function
            fx = self._eval_fun_exprs(av)
            exprs.append(fx)

            for avi in av:
                # Calculate step size for finite difference
                h = self.delta_h*np.abs(avi)
                h[np.where(h == 0)] = self.delta_h

                # Calculate derivatives for functional averages
                avi += h
                df.append((self._eval_fun_exprs(av) - fx) / h)
                avi -= h

            # Stack derivatives
            dfexpr.append(np.array(df))

        # Multiply by variance and take RMS value
        fv = [np.linalg.norm(df*sd[:, None], axis=0)
              for df, sd in zip(dfexpr, dev)]

        return exprs, fv

    def _acc_avg_var(self, intg, currex):
        prevex, vaccex, accex = self.prevex, self.vaccex, self.accex

        # Weights for online variance and average
        Wmp1mpn = intg.tcurr - self.prevt
        W1mpn = intg.tcurr - self.tstart_acc
        Wp = 2*(W1mpn - Wmp1mpn)*W1mpn

        # Iterate over each element type
        for v, a, p, c in zip(vaccex, accex, prevex, currex):
            ppc = p + c

            # Accumulate average
            a += Wmp1mpn*ppc

            # Accumulate variance
            v += Wmp1mpn*(p**2 + c**2 - 0.5*ppc**2)
            if self.tstart_acc != self.prevt:
                v += (Wmp1mpn / Wp)*(a - W1mpn*ppc)**2

    def _prepare_meta(self, intg, std_max, std_sum):
        comm, rank, root = get_comm_rank_root()

        stats = Inifile()
        stats.set('data', 'prefix', 'tavg')
        stats.set('data', 'fields', ','.join(self.outfields))
        stats.set('tavg', 'cfg-section', self.cfgsect)
        stats.set('tavg', 'range', f'[({self.tstart_acc}, {intg.tcurr})]')

        intg.collect_stats(stats)

        # Reduce our standard deviations across ranks
        if rank != root:
            comm.Reduce(std_max, None, op=mpi.MAX, root=root)
            comm.Reduce(std_sum, None, op=mpi.SUM, root=root)

            return None
        else:
            std_max, std_sum = std_max.copy(), std_sum.copy()
            std_max_it, std_sum_it = iter(std_max), iter(std_sum)

            comm.Reduce(mpi.IN_PLACE, std_max, op=mpi.MAX, root=root)
            comm.Reduce(mpi.IN_PLACE, std_sum, op=mpi.SUM, root=root)

            # Write standard deviation stats
            for an, vm, vs in zip(self.anames, std_max_it, std_sum_it):
                stats.set('tavg', f'max-std-{an}', vm)
                stats.set('tavg', f'avg-std-{an}', vs / self.tpts)

            # Followed by functional standard deviation stats
            for fn, fm, fs in zip(self.fnames, std_max_it, std_sum_it):
                stats.set('tavg', f'max-std-fun-{fn}', fm)
                stats.set('tavg', f'avg-std-fun-{fn}', fs / self.tpts)

            return dict(intg.cfgmeta, stats=stats.tostr(),
                        mesh_uuid=intg.mesh_uuid)

    def _write_avg(self, intg):
        accex, vaccex = self.accex, self.vaccex
        nacc, nfun = len(self.anames), len(self.fnames)
        tavg = []

        # Maximum and sum of standard deviations
        std_max, std_sum = np.zeros((2, nacc + nfun))
        std_max_a, std_sum_a = std_max[:nacc], std_sum[:nacc]
        std_max_f, std_sum_f = std_max[nacc:], std_sum[nacc:]

        wts = 2*(intg.tcurr - self.tstart_acc)

        # Normalise the accumulated expressions
        tavg.append([a / wts for a in accex])

        # Calculate their standard deviations
        dev = [np.sqrt(np.abs(v / wts)) for v in vaccex]

        # Reduce these deviations across each element type
        for dx in dev:
            np.maximum(np.amax(dx, axis=(1, 2)), std_max_a,
                        out=std_max_a)
            std_sum_a += dx.sum(axis=(1, 2))

        # Handle any functional expressions
        if self.fexprs:
            # Evaluate functional expressions and standard deviations
            funex, fdev = self._eval_fun_var(dev, tavg[-1])

            # Add in functional expressions
            tavg.append(funex)

            # Reduce these deviations across each element type
            for fx in fdev:
                np.maximum(np.amax(fx, axis=(1, 2)), std_max_f,
                            out=std_max_f)
                std_sum_f += fx.sum(axis=(1, 2))

        # Add in standard deviations
        if self.std_mode == 'all':
            tavg.append(dev)

            # Add in functional expression deviations
            if self.fexprs:
                tavg.append(fdev)

        # Form the output records to be written to disk
        data = dict(self._ele_region_data)

        # Stack together expressions by element type
        tavg = [np.vstack(list(avgs)) for avgs in zip(*tavg)]

        for (idx, etype, rgn), d in zip(self._ele_regions, tavg):
            data[etype] = d.swapaxes(0, 1).astype(self.fpdtype)

        # Prepare the metadata
        metadata = self._prepare_meta(intg, std_max, std_sum)

        # Write to disk and return the file name
        return self._writer.write(data, intg.tcurr, metadata)

    def __call__(self, intg):
        # If we are not supposed to be averaging yet then return
        if intg.tcurr < self.tstart:
            return

        # If necessary, run the start-up routines
        if not self._started:
            self._init_accumex(intg)
            self._started = True

        # See if we are due to write and/or accumulate this step
        dowrite = intg.tcurr - self.tout_last >= self.dtout - self.tol
        doaccum = intg.nacptsteps % self.nsteps == 0

        if dowrite or doaccum:
            # Evaluate the time averaging expressions
            currex = self._eval_acc_exprs(intg)

            # Accumulate them; always do this even when just writing
            self._acc_avg_var(intg, currex)

            # Save the time and solution
            self.prevt = intg.tcurr
            self.prevex = currex

            if dowrite:
                # Write the file out to disk
                solnfname = self._write_avg(intg)

                # If a post-action has been registered then invoke it
                self._invoke_postaction(intg, mesh=intg.system.mesh.fname,
                                        soln=solnfname, t=intg.tcurr)

                # Reset the accumulators
                if self.mode == 'windowed':
                    for a, v in zip(self.accex, self.vaccex):
                        a.fill(0)
                        v.fill(0)

                    self.tstart_acc = intg.tcurr

                self.tout_last = intg.tcurr
