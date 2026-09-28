# An independent audit, in base R, of the statistics the app computes in Python
# (autotradebot/research/significance.py): each setup's expectancy, Tharp's quality number, Aronson's
# bootstrap p-value, and White's reality check across the setups. Two implementations that agree
# are worth more than one that is trusted. Nothing here runs live - the app never waits on R.
#
#   python scripts/export_training_set.py                      # writes data/research/training_set.csv
#   Rscript scripts/r/audit_records.R data/research/training_set.csv [replay|shadow|live]
#
# Needs no packages beyond the ones R ships with.

args <- commandArgs(trailingOnly = TRUE)
path <- if (length(args) >= 1) args[1] else "data/research/training_set.csv"
keep <- if (length(args) >= 2) args[2] else "replay"
rows <- read.csv(path, stringsAsFactors = FALSE)
rows <- rows[rows$source == keep & !is.na(rows$r), ]
cat(sprintf("%d %s rows from %s\n\n", nrow(rows), keep, path))

set.seed(20060926)
draws <- 2000
groups <- split(rows$r, rows$strategy)
groups <- groups[sapply(groups, length) >= 5]

sqn <- function(r) sqrt(min(length(r), 100)) * mean(r) / sd(r)
boot_means <- function(r) replicate(draws, mean(sample(r - mean(r), replace = TRUE)))

nulls <- lapply(groups, boot_means)                         # each setup with its true mean set to zero
p_raw <- mapply(function(r, null) (1 + sum(null >= mean(r))) / (draws + 1), groups, nulls)
t_obs <- sapply(groups, function(r) mean(r) / (sd(r) / sqrt(length(r))))
t_null <- mapply(function(r, null) null / (sd(r) / sqrt(length(r))), groups, nulls)
best <- apply(t_null, 1, max)                               # the best that luck makes of all the setups
p_adj <- sapply(t_obs, function(t) (1 + sum(best >= t)) / (draws + 1))

out <- data.frame(setup = names(groups), trades = sapply(groups, length),
                  expectancy_r = round(sapply(groups, mean), 3), win_rate = round(sapply(groups, function(r) mean(r > 0)), 3),
                  sqn = round(sapply(groups, sqn), 2), p_value = round(p_raw, 3), p_adjusted = round(p_adj, 3),
                  row.names = NULL)
print(out[order(-out$expectancy_r), ], row.names = FALSE)

# the odds the app states against what happened, by decile of the stated probability
if ("probability" %in% names(rows) && sum(!is.na(rows$probability)) > 100) {
  cat("\nstated odds against outcomes (calibration):\n")
  ok <- rows[!is.na(rows$probability), ]
  bins <- cut(ok$probability, breaks = quantile(ok$probability, 0:5 / 5), include.lowest = TRUE)
  print(aggregate(cbind(stated = ok$probability, won = as.numeric(ok$r > 0)) ~ bins, FUN = function(v) round(mean(v), 3)),
        row.names = FALSE)
  fit <- glm(I(r > 0) ~ probability + reward_risk + confidence, data = ok, family = binomial)
  cat("\na plain logistic fit on three readings (slopes near zero = nothing to learn from them yet):\n")
  print(round(summary(fit)$coefficients, 3))
}
