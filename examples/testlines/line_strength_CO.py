import matplotlib.pyplot as plt
from exojax.spec.rtransfer import nugrid
from exojax.spec import moldb, molinfo
from exojax.spec.exomol import gamma_exomol
from exojax.spec import SijT, doppler_sigma, gamma_natural
from exojax.spec import planck
import jax.numpy as jnp
from jax import vmap, jit

N = 1500
# nus,wav,res=nugrid(23365,23385,N,unit="AA")
nus, wav, res = nugrid(23425, 23450, N, unit='AA')
mdbM = moldb.MdbExomol('.database/CO/12C-16O/Li2015', nus)
# loading molecular database
# molmass=molinfo.molmass("CO") #molecular mass (CO)
# mdbM=moldb.MdbExomol('.database/H2O/1H2-16O/POKAZATEL',nus,crit=1.e-45) #loading molecular dat
# molmassM=molinfo.molmass("H2O") #molecular mass (H2O)


q = mdbM.qr_interp(1500.0)
S = SijT(1500.0, mdbM.logsij0, mdbM.nu_lines, mdbM.elower, q)
mask = S > 1.e-28
mdbM.masking(mask)


Tarr = jnp.logspace(jnp.log10(800), jnp.log10(1700), 100)
qt = vmap(mdbM.qr_interp)(Tarr)
SijM = jit(vmap(SijT, (0, None, None, None, 0)))(
    Tarr, mdbM.logsij0, mdbM.nu_lines, mdbM.elower, qt)


imax = jnp.argmax(SijM, axis=0)
Tmax = Tarr[imax]
print(jnp.min(Tmax))

pl = planck.piBarr(jnp.array([1100.0, 1000.0]), nus)
print(pl[1]/pl[0])

pl = planck.piBarr(jnp.array([1400.0, 1200.0]), nus)
print(pl[1]/pl[0])

lsa = ['solid', 'dashed', 'dotted', 'dashdot']
lab = ['D', 'E', 'F']
fac = 1.e22
fig = plt.figure(figsize=(12, 6))
# for i in range(len(mdbM.A)):
#    j=0
for j, i in enumerate([7, 5, 3]):
    w = lab[j]+': '+str(int(1.e8/mdbM.nu_lines[i]))+'$\\AA$'
    plt.plot(Tarr, SijM[:, i]*fac, ls=lsa[j], alpha=1.0, label=w)
    plt.text(Tmax[i], SijM[imax[i], i]*fac-(1-j)*0.3,
             lab[j], fontsize=20, color='C'+str(j))

#    w=str(int(1.e8/mdbM.nu_lines[i]*(1.0+28.0/300000.)))+"AA"+" i="+str(i)
#    plt.plot(Tarr,SijM[:,i]*fac,color="C"+str(j),ls=lsa[j],alpha=1.0,label=w)
#    plt.text(Tmax[i],SijM[imax[i],i]*fac-(1-j)*0.01*i,str(i),fontsize=20,color="C"+str(j))

    # plt.axvspan(1000.0,1100.0,alpha=0.3)
plt.axvspan(1150.0, 1200.0, alpha=0.3)

plt.legend(fontsize=18)

plt.tick_params(labelsize=18)
plt.xlabel('Temperature (K)', fontsize=18)
plt.ylabel('Line strength ($10^{-22}$ cm)', fontsize=18)
plt.title('CO Li2005', fontsize=18)
plt.savefig('lsco.pdf', bbox_inches='tight', pad_inches=0.0)
plt.savefig('lsco.png', bbox_inches='tight', pad_inches=0.0)
plt.show()
