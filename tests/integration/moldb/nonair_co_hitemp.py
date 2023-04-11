# %%
from exojax.spec import api
from exojax.utils.grids import wavenumber_grid

nus, wav, r = wavenumber_grid(24000.0, 26000.0, 1000, unit="AA", xsmode="premodit")

# when
mdb = api.MdbHitran("CO", nus, activation=False)

# %%
from exojax.spec.qstate import m_transition_state
from exojax.spec.nonair import gamma_nonair, temperature_exponent_nonair
from exojax.spec.nonair import nonair_coeff_CO_in_H2
m = m_transition_state(mdb.df["jl"],mdb.df["branch"]).values
n_Texp_H2 = temperature_exponent_nonair(m, nonair_coeff_CO_in_H2)
gamma_ref_H2v = gamma_nonair(m, nonair_coeff_CO_in_H2)
print(n_Texp_H2)


# %%
import matplotlib.pyplot as plt
import numpy as np
print(np.unique(mdb.df["branch"].values))

# %%
from exojax.spec.qstate import m_transition_state
m_transition_state(mdb.df["jl"],mdb.df["branch"]).values

# %% 
import matplotlib.pyplot as plt
for dv in range(0, 6):
    mask = mdb.df["vu"] - mdb.df["vl"] == dv
    dfv = mdb.df[mask]
    plt.plot(1.e4 / dfv["wav"].values,
             dfv["int"].values,
             ".",
             label="$\\Delta \\nu = $" + str(dv),
             alpha=0.2)
#plt.show()

load_mask = (mdb.df["vu"] - mdb.df["vl"] == 3)
mdb.activate(mdb.df, load_mask)
plt.plot(1.e4 / mdb.nu_lines,
         mdb.Sij0,
         "+",
         color="black",
         label="activated lines")
plt.legend()
#plt.title(emf)
plt.xlim(2.0,3.0)
plt.ylabel("line strength at 296 K")
plt.xlabel("micron")
plt.yscale("log")
#plt.xscale("log")
plt.show()

# %%
